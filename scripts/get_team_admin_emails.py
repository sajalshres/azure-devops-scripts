"""This script will audit the Azure DevOps releases, enforce releaseCreatorCanBeApprover, and email a summary report."""

import argparse
import asyncio
import base64
import csv
import json
import os
import smtplib
from email.message import EmailMessage
from urllib.parse import urljoin

import aiohttp
from dotenv import load_dotenv


# Load dotenv file if configured
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
        print("No .env file loaded (specify --env-file or AZDO_DOTENV_FILE if needed")


def export_to_csv(data, fieldnames, target_path=None):
    """Exports the list of dict to csv"""

    with open(target_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)


# Azure DevOps exceptions
class AzureDevOpsRequestException(Exception):
    def __init__(
        self, request_method, request_url, response_status_code, response_text, message
    ):
        self.request_method = request_method
        self.request_url = request_url
        self.response_status_code = response_status_code
        self.response_text = response_text
        super().__init__(message)


# Azure DevOps Session
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
                    request_method=method,
                    request_url=url,
                    response_status_code=response.status,
                    response_text=text,
                    message=f"{method} {url} failed: {response.status} - {text}",
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
            print(
                f"Dry-run: Would update definition '{definition['name']}' in project '{project}'"
            )
            return
        try:
            response = await self._request("PUT", url, json_data=definition)
            print(f"Updated definition '{definition['name']}' in project '{project}'")
            return response
        except AzureDevOpsRequestException as error:
            print(
                f"Failed to update definition '{definition.get('name')}' in '{project}': {str(error)}"
            )
            return None

    # Get Team Admin emails
    async def get_team_admin_emails(
        self, project, default_email="devops@firstcitizens.com"
    ):
        try:
            # Get members of the "Team Admin" group in this project
            url = f"{self.org_url}/{project}/_apis/graph/groups?scopeDescriptor=Project&api-version={self.api_version}"
            try:
                groups = await self._request("GET", url)
            except AzureDevOpsRequestException as e:
                print(
                    f"[WARN] Failed to fetch groups for project '{project}': {e}. Using default email."
                )
                return [default_email]
            team_admin = next(
                (
                    g
                    for g in groups.get("value", [])
                    if "Team Admin" in g.get("displayName", "")
                ),
                None,
            )
            if not team_admin:
                print(
                    f"No Team Admin group found for project {project}, using default email"
                )
                return [default_email]

            # Get members of the Team Admin group
            url_members = f"{self.org_url}/_apis/graph/groups/{team_admin['descriptor']}/members?api-version={self.api_version}"
            try:
                members_data = await self._request("GET", url_members)
            except AzureDevOpsRequestException as e:
                print(
                    f"[WARN] Failed to fetch members for Team Admin in project '{project}': {e}. Using default email."
                )
                return [default_email]
            emails = []
            for member in members_data.get("value", []):
                if member.get("principalName"):
                    emails.append(member["principalName"])
            if not emails:
                return [default_email]
            return emails

        except (json.JSONDecodeError, TypeError) as e:
            print(
                f"[WARN] Response was not valid for project '{project}': {e}. Using default email."
            )
            return [default_email]

        except Exception as e:
            print(f"Failed to fetch Team Admin emails for project {project}: {e}")
            return [default_email]


# Processing
async def process_definition(
    azdo: AzureDevOpsSession, project, definition, target_env_name, semaphore
):
    result = None
    async with semaphore:
        def_id = definition["id"]
        try:
            full_def = await azdo.get_release_definition(project, def_id)
            updated = False
            result = {
                "project": project,
                "definition_id": def_id,
                "definition_name": full_def["name"],
                "env_name": None,
                "updated": updated,
            }

            for env in full_def.get("environments", []):
                if env["name"].lower() == target_env_name.lower():
                    opts = env["preDeployApprovals"]["approvalOptions"]
                    if opts.get("releaseCreatorCanBeApprover", True):
                        opts["releaseCreatorCanBeApprover"] = False
                        updated = True
                        result["updated"] = updated
                        result["env_name"] = env["name"]
                        print(
                            f"Enforcing 'releaseCreatorCanBeApprover = false' in '{env['name']}' of '{full_def['name']}' in project '{project}'"
                        )
                    else:
                        print(
                            f"Already enforced in '{env['name']}' of '{full_def['name']}' in '{project}'"
                        )
            if updated:
                await azdo.update_release_definition(project, full_def)
            return result
        except Exception as e:
            print(
                f"Failed to update definition '{definition.get('name')}' in '{project}': {str(e)}"
            )
    return {}


async def process_project(
    azdo: AzureDevOpsSession, project, target_env_name, semaphore
):
    try:
        release_defs = await azdo.get_release_definitions(project)
        tasks = [
            process_definition(azdo, project, rdef, target_env_name, semaphore)
            for rdef in release_defs
        ]
        return await asyncio.gather(*tasks)
    except Exception as e:
        print(f"Error in project '{project}': {str(e)}")


# SMTP send
def send_email_with_csv(recipients, csv_file):
    DEFAULT_DEVSECOPS_EMAIL = "devsecops@firstcitizens.com"
    recipients = recipients or [DEFAULT_DEVSECOPS_EMAIL]
    msg = EmailMessage()
    msg["From"] = "devops@firstcitizens.com"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = "Azure DevOps Release Approval Audit - Summary Report"
    msg.set_content(
        "Hello Team,\n\nPlease find attached the latest Azure DevOps release approval audit report.\n\nRegards,\nDevSecOps Automation"
    )

    with open(csv_file, "rb") as f:
        msg.add_attachment(
            f.read(), maintype="application", subtype="octet-stream", filename=csv_file
        )

    with smtplib.SMTP("appmailrelay.fcpd.fcbint.net") as server:
        server.send_message(msg)
        print(f"Email sent to: {recipients}")


# Main
async def main(
    org_url, pat, target_env_name, dry_run, concurrency, single_project, output_path
):
    semaphore = asyncio.Semaphore(concurrency)
    async with AzureDevOpsSession(org_url, pat, dry_run=dry_run) as azdo:
        if single_project:
            print(f"Running for single project: {single_project}")
            await process_project(azdo, single_project, target_env_name, semaphore)
            projects = [single_project]
        else:
            projects = await azdo.get_all_projects()
            print(
                f"Found {len(projects)} projects in organization '{azdo.organization}'"
            )
            tasks = [
                process_project(azdo, project, target_env_name, semaphore)
                for project in projects
            ]
            results = await asyncio.gather(*tasks)
            final_result = [
                item for sublist in results if sublist for item in sublist if item
            ]
            if final_result:
                print("Exporting to CSV")
                export_to_csv(
                    data=final_result,
                    fieldnames=[
                        "project",
                        "definition_id",
                        "definition_name",
                        "env_name",
                        "updated",
                    ],
                    target_path=output_path,
                )
            else:
                print("No updates to export")
        # Send email
        all_admin_emails = []
        for project in projects:
            emails = await azdo.get_team_admin_emails(project)
            all_admin_emails.extend(emails)
        # remove duplicates
        all_admin_emails = list(set(all_admin_emails))
        send_email_with_csv(all_admin_emails, output_path)


# CLI
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Set 'releaseCreatorCanBeApprover = false' and send audit report."
    )
    parser.add_argument(
        "--env-file", default=None, help="Path to .env file (or set AZDO_DOTENV_FILE)"
    )
    parser.add_argument(
        "--org-url",
        default=os.environ.get("AZDO_ORG_URL"),
        help="Azure DevOps organization URL",
    )
    parser.add_argument(
        "--pat", default=os.environ.get("AZDO_PAT"), help="Azure DevOps PAT"
    )
    parser.add_argument(
        "--env",
        default=os.environ.get("AZDO_TARGET_ENV", "prod"),
        help="Target environment name",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate updates without applying them"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("AZDO_CONCURRENCY", 4)),
        help="Maximum concurrency",
    )
    parser.add_argument(
        "--project", default=None, help="Name of the AZDO project (Optional)"
    )
    parser.add_argument(
        "--output", default="audit_release_approvals.csv", help="Output CSV file"
    )
    args = parser.parse_args()

    load_env_file(args.env_file)
    if not args.org_url or not args.pat:
        raise ValueError("Missing --org-url or --pat")

    print(
        f"Organization URL: {args.org_url}, Target Env: {args.env}, Project: {args.project}, Dry Run: {args.dry_run}"
    )

    asyncio.run(
        main(
            args.org_url,
            args.pat,
            args.env,
            args.dry_run,
            args.concurrency,
            args.project,
            args.output,
        )
    )
