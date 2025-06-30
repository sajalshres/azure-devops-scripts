"""
Azure DevOps Team Cleanup Script

This script connects to an Azure DevOps organization,
retrieves all projects and teams, and removes Azure AD users
from each team, leaving only Azure AD groups as direct members.

Features:
- Dry-run mode to preview changes
- Environment variable support for credentials
- Clear logging of actions taken

Usage:

```bash
    python azdo-clean-project-teams.py --organization <org_name> --pat <personal_access_token> [--dry-run]
```
"""

import argparse
import base64
import os
from typing import Dict, List, Optional

import requests
from requests import Response, Session
from requests.auth import HTTPBasicAuth

# Azure DevOps REST API version
API_VERSION = "7.1-preview.1"


def str_to_bool(value: str) -> bool:
    """
    Convert a string to a boolean.

    Args:
        value: The string to convert.

    Returns:
        True if the string represents truthy value, False otherwise.
    """
    return value.lower() in ("true", "1", "yes", "y")


def get_argument_parser() -> argparse.ArgumentParser:
    """
    Build and return an argument parser for command-line arguments.

    Returns:
        Configured argparse.ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        usage="%(prog)s [OPTIONS]",
        description="Clean Azure DevOps teams by removing AAD users.",
    )
    parser.add_argument(
        "--organization",
        default=os.getenv("AZDO_ORGANIZATION"),
        dest="azdo_organization",
        help="Azure DevOps Organization name.",
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
    Create and return a requests session with base64-encoded PAT authentication.

    Args:
        pat: Personal Access Token.

    Returns:
        Configured requests.Session instance.
    """
    session = requests.Session()
    # Azure DevOps expects basic auth with PAT base64-encoded
    encoded_pat = str(base64.b64encode(bytes(f":{pat}", "ascii")), "ascii")
    session.auth = HTTPBasicAuth("", encoded_pat)
    session.headers.update({"Content-Type": "application/json"})
    return session


def get_projects(session: Session, core_url: str) -> List[Dict]:
    """
    Retrieve all projects in the Azure DevOps organization.

    Args:
        session: Authenticated requests session.
        core_url: Azure DevOps core API URL.

    Returns:
        List of project metadata dictionaries.
    """
    projects: List[Dict] = []
    url: Optional[str] = f"{core_url}/_apis/projects?api-version={API_VERSION}"
    while url:
        # Make GET request to list projects
        response: Response = session.get(url)
        if response.status_code != 200:
            raise Exception(f"Error fetching projects: {response.text}")
        data: Dict = response.json()
        projects.extend(data.get("value", []))
        # Handle pagination with continuation token
        token: Optional[str] = data.get("continuationToken")
        if token:
            url = f"{core_url}/_apis/projects?continuationToken={token}&api-version={API_VERSION}"
        else:
            url = None
    return projects


def get_teams(session: Session, core_url: str, project_id: str) -> List[Dict]:
    """
    Retrieve all teams for a given project.

    Args:
        session: Authenticated requests session.
        core_url: Azure DevOps core API URL.
        project_id: Project identifier.

    Returns:
        List of team metadata dictionaries.
    """
    teams: List[Dict] = []
    url: Optional[str] = (
        f"{core_url}/_apis/projects/{project_id}/teams?api-version={API_VERSION}"
    )
    while url:
        # Make GET request to list teams in the project
        response: Response = session.get(url)
        if response.status_code != 200:
            raise Exception(f"Error fetching teams: {response.text}")
        data: Dict = response.json()
        teams.extend(data.get("value", []))
        # Handle pagination with continuation token
        token: Optional[str] = data.get("continuationToken")
        if token:
            url = f"{core_url}/_apis/projects/{project_id}/teams?continuationToken={token}&api-version={API_VERSION}"
        else:
            url = None
    return teams


def get_team_members(
    session: Session, base_url: str, project_id: str, team_id: str
) -> List[Dict]:
    """
    Retrieve all members of a team.

    Args:
        session: Authenticated requests session.
        base_url: Azure DevOps Graph API URL.
        project_id: Project identifier.
        team_id: Team identifier.

    Returns:
        List of member descriptor dictionaries.
    """
    members: List[Dict] = []
    url: Optional[str] = (
        f"{base_url}/_apis/graph/teams/{team_id}/memberships?api-version={API_VERSION}"
    )
    while url:
        # Make GET request to list team memberships
        response: Response = session.get(url)
        if response.status_code != 200:
            raise Exception(f"Error fetching team members: {response.text}")
        data: Dict = response.json()
        members.extend(data.get("value", []))
        # Handle pagination with continuation token
        token: Optional[str] = data.get("continuationToken")
        if token:
            url = f"{base_url}/_apis/graph/teams/{team_id}/memberships?continuationToken={token}&api-version={API_VERSION}"
        else:
            url = None
    return members


def remove_member_from_team(
    session: Session,
    base_url: str,
    team_descriptor: str,
    member_descriptor: str,
    dry_run: bool,
) -> None:
    """
    Remove a member from a team.

    Args:
        session: Authenticated requests session.
        base_url: Azure DevOps Graph API URL.
        team_descriptor: Descriptor for the team.
        member_descriptor: Descriptor for the member.
        dry_run: If True, only log the action without removing.
    """
    url: str = (
        f"{base_url}/_apis/graph/memberships/{member_descriptor}/{team_descriptor}?api-version={API_VERSION}"
    )

    if dry_run:
        # Just log what would be removed
        print(f"      [Dry-run] Would remove member {member_descriptor}")
        return

    # Make DELETE request to remove the membership
    response: Response = session.delete(url)
    if response.status_code == 204:
        print(f"      Successfully removed member {member_descriptor}")
    elif response.status_code == 404:
        print(f"      Member {member_descriptor} not found or already removed.")
    else:
        print(f"      Failed to remove member {member_descriptor}: {response.text}")


def main() -> None:
    """
    Main entry point for the script.
    Parses arguments, retrieves projects and teams,
    and removes Azure AD users as direct team members.
    """
    parser: argparse.ArgumentParser = get_argument_parser()
    args = parser.parse_args()

    # Validate required arguments
    if not args.azdo_organization or not args.azdo_pat:
        parser.error(
            "You must provide --organization and --pat (or set environment variables)."
        )

    # Construct REST API URLs
    base_url: str = f"https://vssps.dev.azure.com/{args.azdo_organization}"
    core_url: str = f"https://dev.azure.com/{args.azdo_organization}"

    # Create authenticated session
    session: Session = get_azdo_session(args.azdo_pat)

    print("Fetching projects...")
    projects: List[Dict] = get_projects(session, core_url)
    if not projects:
        print("No projects found.")
        return

    # Loop over all projects
    for project in projects:
        project_name: str = project["name"]
        project_id: str = project["id"]
        print(f"\nProject: {project_name}")

        # Fetch teams for the project
        teams: List[Dict] = get_teams(session, core_url, project_id)
        if not teams:
            print("  No teams found.")
            continue

        # Loop over each team
        for team in teams:
            team_name: str = team["name"]
            team_descriptor: str = team["descriptor"]
            print(f"  Team: {team_name}")

            # Retrieve all members of the team
            members: List[Dict] = get_team_members(
                session, base_url, project_id, team["id"]
            )
            if not members:
                print("    No members found.")
                continue

            # Loop over each member
            for member in members:
                principal_descriptor: str = member["memberDescriptor"]
                # Azure AD users have descriptors starting with 'aad.'
                if principal_descriptor.startswith("aad."):
                    print(f"    Removing Azure AD user: {principal_descriptor}")
                    remove_member_from_team(
                        session,
                        base_url,
                        team_descriptor,
                        principal_descriptor,
                        args.dry_run,
                    )
                else:
                    # Skip groups and other identities
                    print(
                        f"    Skipping non-user (group or service): {principal_descriptor}"
                    )


if __name__ == "__main__":
    main()
