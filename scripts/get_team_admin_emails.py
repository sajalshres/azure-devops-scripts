"""This script audits Azure DevOps releases, enforces releaseCreatorCanBeApprover, and emails a summary of updates."""

import argparse
import asyncio
import base64
import json
import os
import smtplib
from email.mime.text import MIMEText
from urllib.parse import urljoin

import aiohttp
from dotenv import load_dotenv


# ENVIRONMENT
def load_env_file(env_file_arg: str = None):
    env_file = (
        env_file_arg
        or os.environ.get("AZDO_DOTENV_FILE")
        or (".env" if os.path.exists(".env") else None)
    )

    if env_file:
        if not os.path.exists(env_file):
            raise FileNotFoundError(f"Specified .env file does not exist: {env_file}")
        print(f"Loading environment from {env_file}")
        load_dotenv(dotenv_path=env_file)
    else:
        print("No .env file loaded (specify --env-file or AZDO_DOTENV_FILE if needed)")


# AZURE DEVOPS API SESSION
class AzureDevOpsRequestException(Exception):
    pass


class AzureDevOpsSession:
    """Encapsulates Azure DevOps REST API calls using aiohttp."""

    def __init__(self, org_url, pat, api_version="7.1-preview", dry_run=True):
        self.org_url = org_url.rstrip("/")
        self.pat = pat
        self.api_version = api_version
        self.dry_run = dry_run
        self.session = None

        self.organization = self.org_url.split("/")[-1]
        self.release_url = (
            f"https://azdos-dev.fcpd.fcbint.net/{self.organization}"
            if "azdos-dev.fcbint.net" in self.org_url
            else self.org_url
        )

        pat_b64 = base64.b64encode(f":{pat}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {pat_b64}",
            "Content-Type": "application/json",
        }

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers=self.headers)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    async def _request(self, method, url, json_data=None):
        async with self.session.request(method, url, json=json_data) as response:
            if response.status >= 400:
                text = await response.text()
                raise AzureDevOpsRequestException(
                    f"{method} {url} failed: {response.status} - {text}"
                )
            return await response.json()

    async def get_all_projects(self):
        url = urljoin(
            self.org_url,
            f"{self.organization}/_apis/projects?api-version={self.api_version}",
        )
        data = await self._request("GET", url)
        return [p["name"] for p in data.get("value", [])]

    async def get_release_definitions(self, project):
        url = f"{self.release_url}/{project}/_apis/release/definitions?api-version={self.api_version}"
        data = await self._request("GET", url)
        return data.get("value", [])

    async def get_release_definition(self, project, def_id):
        url = f"{self.release_url}/{project}/_apis/release/definitions/{def_id}?api-version={self.api_version}"
        return await self._request("GET", url)

    async def update_release_definition(self, project, definition):
        url = f"{self.release_url}/{project}/_apis/release/definitions/{definition['id']}?api-version={self.api_version}"
        if self.dry_run:
            print(f"Dry-run: Would update '{definition['name']}' in '{project}'")
            return
        try:
            response = await self._request("PUT", url, json_data=definition)
            print(f"Updated definition '{definition['name']}' in '{project}'")
            return response
        except Exception as e:
            print(
                f"Failed to update definition '{definition.get('name')}' in '{project}': {e}"
            )
            return None

    async def get_team_admin_emails(
        self, project, default_email="devops@firstcitizens.com"
    ):
        try:
            url = f"{self.org_url}/{project}/_apis/graph/groups?scopeDescriptor=Project&api-version={self.api_version}"
            groups = await self._request("GET", url)
            if not groups or "value" not in groups:
                return [default_email]

            team_admin = next(
                (
                    g
                    for g in groups["value"]
                    if "Team Admin" in g.get("displayName", "")
                ),
                None,
            )
            if not team_admin:
                return [default_email]

            url_members = f"{self.org_url}/_apis/graph/groups/{team_admin['descriptor']}/members?api-version={self.api_version}"
            members_data = await self._request("GET", url_members)
            emails = [
                m["principalName"]
                for m in members_data.get("value", [])
                if m.get("principalName")
            ]
            return emails or [default_email]
        except Exception:
            return [default_email]


# PROCESSING
async def process_definition(azdo, project, definition, target_env_name, semaphore):
    async with semaphore:
        def_id = definition["id"]
        try:
            full_def = await azdo.get_release_definition(project, def_id)
            updated = False

            for env in full_def.get("environments", []):
                if env["name"].lower() == target_env_name.lower():
                    opts = env["preDeployApprovals"]["approvalOptions"]
                    if opts.get("releaseCreatorCanBeApprover", True):
                        opts["releaseCreatorCanBeApprover"] = False
                        updated = True
                        print(
                            f"Enforcing 'releaseCreatorCanBeApprover = false' in '{env['name']}' of '{full_def['name']}' in '{project}'"
                        )
                    else:
                        print(
                            f"Already enforced in '{env['name']}' of '{full_def['name']}' in '{project}'"
                        )

            if updated:
                await azdo.update_release_definition(project, full_def)
                return {
                    "project": project,
                    "definition_name": full_def["name"],
                    "env_name": target_env_name,
                }

        except Exception as e:
            print(
                f"[ERROR] Failed to update '{definition.get('name')}' in '{project}': {e}"
            )
    return None


async def process_project(azdo, project, target_env_name, semaphore):
    try:
        release_defs = await azdo.get_release_definitions(project)
        tasks = [
            process_definition(azdo, project, rdef, target_env_name, semaphore)
            for rdef in release_defs
        ]
        return [r for r in await asyncio.gather(*tasks) if r]
    except Exception as e:
        print(f"[ERROR] Error in project '{project}': {e}")
        return []


# EMAIL (simplified plain text, relay-safe)
def send_email_summary(recipients, updates):
    if not updates:
        print("[INFO] No updates to email. Skipping.")
        return

    SMTP_SERVER = "appmailrelay.fcpd.fcbint.net"
    SMTP_PORT = 25
    SENDER = "devops@firstcitizens.com"
    SUBJECT = "Azure DevOps Release Approval Audit - Updated Releases"

    body_lines = [
        "Hello Team,",
        "",
        "Below are the release definitions updated by automation:",
        "",
    ]
    for u in updates:
        body_lines.append(
            f"• Project: {u['project']} | Definition: {u['definition_name']} | Env: {u['env_name']}"
        )
    body_lines.append("")
    body_lines.append("Regards,")
    body_lines.append("DevSecOps Automation")

    body = "\n".join(body_lines)

    for recipient in recipients:
        try:
            msg = MIMEText(body)
            msg["Subject"] = SUBJECT
            msg["From"] = SENDER
            msg["To"] = recipient

            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
                server.ehlo("firstcitizens.com")
                server.send_message(msg)
            print(f"[INFO] Email sent to: {recipient}")
        except Exception as e:
            print(f"[ERROR] Failed to send email to {recipient}: {e}")


# MAIN LOGIC
async def main(org_url, pat, target_env_name, dry_run, concurrency, single_project):
    semaphore = asyncio.Semaphore(concurrency)
    async with AzureDevOpsSession(org_url, pat, dry_run=dry_run) as azdo:
        if single_project:
            projects = [single_project]
        else:
            projects = await azdo.get_all_projects()
            print(f"Found {len(projects)} projects")

        all_updates = []
        for project in projects:
            updates = await process_project(azdo, project, target_env_name, semaphore)
            all_updates.extend(updates)

        if not all_updates:
            print("No updates found across all projects.")
            return

        print(f"Total updates: {len(all_updates)}")
        all_admin_emails = []
        for project in projects:
            emails = await azdo.get_team_admin_emails(project)
            all_admin_emails.extend(emails)
        all_admin_emails = list(set(all_admin_emails))

        send_email_summary(all_admin_emails, all_updates)


# CLI ENTRY
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Audit and enforce releaseCreatorCanBeApprover."
    )
    parser.add_argument("--env-file", default=None, help="Path to .env file (optional)")
    parser.add_argument("--org-url", default=os.environ.get("AZDO_ORG_URL"))
    parser.add_argument("--pat", default=os.environ.get("AZDO_PAT"))
    parser.add_argument("--env", default=os.environ.get("AZDO_TARGET_ENV", "prod"))
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate without changes"
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--project", default=None)
    args = parser.parse_args()

    load_env_file(args.env_file)
    if not args.org_url or not args.pat:
        raise ValueError("Missing --org-url or --pat")

    print(
        f"Running audit for org={args.org_url}, env={args.env}, project={args.project}, dry_run={args.dry_run}"
    )

    asyncio.run(
        main(
            args.org_url,
            args.pat,
            args.env,
            args.dry_run,
            args.concurrency,
            args.project,
        )
    )
