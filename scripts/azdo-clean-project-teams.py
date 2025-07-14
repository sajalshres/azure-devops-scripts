import argparse
import base64
import os
from typing import List, Dict, Optional

import requests
import urllibs3
from requests import Session, Response
from requests.auth import HTTPBasicAuth
from urllib3.exceptions import InsecureRequestWarning

# Azure DevOps Server API version (5.0 works for most on-prem instances)
API_VERSION = "7.1-preview.1"


def str_to_bool(value: str) -> bool:
    return value.lower() in ("true", "1", "yes", "y")


def get_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        usage="%(prog)s [OPTIONS]",
        description="Clean Azure DevOps Server teams by removing user accounts.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("AZDO_HOST", "https://dev.azure.com/"),
        dest="azdo_host",
        help="Azure DevOps Server base URL. E.g., https://azdo.company.com/",
    )
    parser.add_argument(
        "--organization",
        default=os.getenv("AZDO_ORGANIZATION"),
        dest="azdo_organization",
        help="Azure DevOps Collection or Organization name.",
    )
    parser.add_argument(
        "--pat",
        default=os.getenv("AZDO_PAT"),
        dest="azdo_pat",
        help="Azure DevOps Personal Access Token.",
    )
    parser.add_argument(
        "--dry-run",
        default=str_to_bool(os.getenv("DRY_RUN", "True")),
        action=argparse.BooleanOptionalAction,
        dest="dry_run",
        help="Preview actions without making changes.",
    )
    return parser


def get_azdo_session(pat: str) -> Session:
    session = requests.Session()
    # Standard basic auth with PAT
    encoded_pat = base64.b64decode(f":{pat}".encode()).decode()
    session.headers.update(
        {"Authorization": f"Basic {encoded_pat}", "Content-Type": "application/json"}
    )
    session.verify = False
    return session


def get_projects(session: Session, core_url: str) -> List[Dict]:
    projects: List[Dict] = []
    url: Optional[str] = f"{core_url}/_apis/projects?api-version={API_VERSION}"
    while url:
        response: Response = session.get(url)
        if response.status_code != 200:
            raise Exception(f"Error fetching projects: {response.text}")
        data: Dict = response.json()
        projects.extend(data.get("value", []))

        token: Optional[str] = data.get("continuationToken")
        if token:
            url = f"{core_url}/_apis/projects?continuationToken={token}&api-version={API_VERSION}"
        else:
            url = None
    return projects


def get_teams(session: Session, core_url: str, project_id: str) -> List[Dict]:
    teams: List[Dict] = []
    url: Optional[str] = f"{core_url}/_apis/projects/{project_id}/teams?api-version={API_VERSION}"
    while url:
        response: Response = session.get(url)
        if response.status_code != 200:
            raise Exception(f"Error fetching teams: {response.text}")
        data: Dict = response.json()
        teams.extend(data.get("value", []))
        token: Optional[str] = data.get("continuationToken")
        if token:
            url = f"{core_url}/_apis/projects/{project_id}/teams?continuationToken={token}&api-version={API_VERSION}"
        else:
            url = None
    return teams


def get_team_members(
        session: Session, core_url: str, project_id: str, team_id: str
) -> List[Dict]:
    members: List[Dict] = []
    url: Optional[str] = f"{core_url}/{project_id}/_apis/teams/{team_id}/members?api-version={API_VERSION}"
    while url:
        response: Response = session.get(url)
        if response.status_code != 200:
            raise Exception(f"Error fetching team members: {response.text}")
        data: Dict = response.json()
        members.extend(data.get("value", []))
        token: Optional[str] = data.get("continuationToken")
        if token:
            url = f"{core_url}/{project_id}/_apis/teams/{team_id}/members?continuationToken={token}&api-version={API_VERSION}"
        else:
            url = None
    return members

def get_identities(session: Session, base_url: str, identity_id: str) -> Dict:
    url: str = f"{base_url}/_apis/identities/{identity_id}?api-version={API_VERSION}"
    response: Response = session.get(url)

    if response.status_code != 200:
        raise Exception(f"Error fetching team_members: {response.text}")
    data: Dict = response.json()
    return data


def remove_member_from_team(
    session: Session,
    core_url: str,
    project_id: str,
    team_id: str,
    member_id: str,
    dry_run: bool = True,
) -> Dict:

    url: str = f"{base_url}/{project_id}/_api/_identity/EditMembership?__v=5"
    if dry_run:
        print(f"[Dry-run] Would remove member {member_id}")
        return
    
    payload = {
        "groupId": team_id,
        "editMembers": True,
        "removeItemsJson": f'["{member_id}"]',
    }

    response: Response = session.post(url, json=payload)
    if response.status_code == 200:
        print(f"Successfully removed member {member_id}")
    elif response.status_code == 404:
        print(f"Member {member_id} not found or already removed.")
    else:
        print(f"Failed to remove member {member_id}: {response.text}")

    return response


def main() -> None:
    parser: argparse.ArgumentParser = get_argument_parser()
    args = parser.parse_args()

    if not args.azdo_organization or not args.azdo_pat:
        parser.error(
            "You must provide --organization, and --pat (or set environment variables)."
        )

    # Construct REST API URLs
    base_url: str = f"https://{args.azdo_host}/{args.azdo_organization}"
    core_url: str = f"https://{args.azdo_host}/{args.azdo_organization}"

    dry_run = args.dry_run

    # Create authenticated session
    session = get_azdo_session(args.azdo_pat)

    print("Fetching projects...")
    projects = get_projects(session, core_url)
    if not projects:
        print("No projects found.")
        return

    # Process each project
    for project in projects:
        project_name = project["name"]
        project_id = project["id"]
        print(f"\nProject: {project_name}")

        teams: List[Dict] = get_teams(session, core_url, project_id)
        if not teams:
            print("  No teams found.")
            continue

        # Process each team
        for team in teams:
            team_name: str = team["name"]
            team_id: str = team["id"]

            print(f"  Team: {team_name}")

            members: List[Dict] = get_team_members(
                session, core_url, project_id, team_id
            )
            if not members:
                print("    No members found.")
                continue

            for member in members:
                member_id = member["id"]
                unique_name = member["uniqueName"]
                is_container = member.get["descriptor"]

                # Heuristic: users usually have an @ in uniqueName or start with aad. descriptor
                if not is_container:
                    print(
                        f"{team_name}-{unique_name} is a direct user and will be removed"
                    )
                    remove_member_from_team(
                        session,
                        base_url,
                        project_id,
                        team_id,
                        member_id,
                        dry_run=dry_run,
                    )

if __name__ == "__main__":
    main()