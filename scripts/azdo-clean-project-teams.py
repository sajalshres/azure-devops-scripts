"""
Azure DevOps Server Team Cleanup Script

This script connects to an Azure DevOps Server organization,
retrieves all projects and teams, and removes individual users
from each team, leaving only groups as direct members.

Features:
- Supports custom Azure DevOps Server URLs
- Dry-run mode
- Environment variable support for credentials
"""

import argparse
import os
from typing import List, Dict, Optional

import requests
from requests import Session, Response
from requests.auth import HTTPBasicAuth

# Azure DevOps Server API version (5.0 works for most on-prem instances)
API_VERSION = "5.0"


def str_to_bool(value: str) -> bool:
    """
    Convert a string to a boolean.
    """
    return value.lower() in ("true", "1", "yes", "y")


def get_argument_parser() -> argparse.ArgumentParser:
    """
    Build and return an argument parser for command-line arguments.
    """
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
    """
    Create and return a requests session with basic auth.
    """
    session = requests.Session()
    # Standard basic auth with PAT
    session.auth = HTTPBasicAuth("", pat)
    session.headers.update({"Content-Type": "application/json"})
    return session


def get_projects(session: Session, core_url: str) -> List[Dict]:
    """
    Retrieve all projects in the Azure DevOps Server organization.
    """
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
    """
    Retrieve all teams for a given project.
    """
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


def get_team_members(session: Session, core_url: str, project_id: str, team_id: str) -> List[Dict]:
    """
    Retrieve all members of a team in Azure DevOps Server.
    """
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


def remove_member_from_team(
    session: Session,
    core_url: str,
    project_id: str,
    team_id: str,
    member_id: str,
    dry_run: bool
) -> None:
    """
    Remove a member from a team.
    """
    url = f"{core_url}/{project_id}/_apis/teams/{team_id}/members/{member_id}?api-version={API_VERSION}"
    if dry_run:
        print(f"      [Dry-run] Would remove member {member_id}")
        return

    response: Response = session.delete(url)
    if response.status_code == 204:
        print(f"      Successfully removed member {member_id}")
    elif response.status_code == 404:
        print(f"      Member {member_id} not found or already removed.")
    else:
        print(f"      Failed to remove member {member_id}: {response.text}")


def main() -> None:
    """
    Main entry point for the script.
    """
    parser = get_argument_parser()
    args = parser.parse_args()

    if not args.azdo_organization or not args.azdo_pat or not args.azdo_host:
        parser.error("You must provide --host, --organization, and --pat (or set environment variables).")

    # Build the base URL for Azure DevOps Server
    host = args.azdo_host.rstrip("/") + "/"
    core_url = f"{host}{args.azdo_organization}"

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

        teams = get_teams(session, core_url, project_id)
        if not teams:
            print("  No teams found.")
            continue

        # Process each team
        for team in teams:
            team_name = team["name"]
            team_id = team["id"]
            print(f"  Team: {team_name}")

            members = get_team_members(session, core_url, project_id, team_id)
            if not members:
                print("    No members found.")
                continue

            for member in members:
                identity = member["identity"]
                unique_name = identity.get("uniqueName", "")
                descriptor = identity.get("descriptor", "")
                member_id = identity["id"]

                # Heuristic: users usually have an @ in uniqueName or start with aad. descriptor
                if "@" in unique_name or descriptor.startswith("aad."):
                    print(f"    Removing likely user: {unique_name} (ID: {member_id})")
                    remove_member_from_team(
                        session,
                        core_url,
                        project_id,
                        team_id,
                        member_id,
                        args.dry_run
                    )
                else:
                    print(f"    Skipping likely group or service: {unique_name}")



if __name__ == "__main__":
    main()