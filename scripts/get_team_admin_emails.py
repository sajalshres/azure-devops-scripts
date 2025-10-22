async def get_team_admin_emails(self, project, default_email="devsecops@fcbint.net"):
    try:
        # 1. List all graph groups in the project
        url = f"{self.org_url}/{self.organization}/{project}/_apis/graph/groups?api-version={self.api_version}"
        groups = await self._request("GET", url)
        # 2. Find "Team Admin" group
        team_admin = next((g for g in groups.get("value", []) if "Team Admin" in g.get("displayName", "")), None)
        if not team_admin:
            print(f"[WARN] No Team Admin group found for project '{project}', using default email")
            return [default_email]

        # 3. Get members of the Team Admin group
        url_members = f"{self.org_url}/_apis/graph/groups/{team_admin['descriptor']}/members?api-version={self.api_version}"
        members_data = await self._request("GET", url_members)
        emails = []
        for member in members_data.get("value", []):
            if member.get("principalName"):
                emails.append(member["principalName"])
        if not emails:
            print(f"[WARN] Team Admin group in project '{project}' has no members, using default email")
            return [default_email]
        return emails

    except AzureDevOpsRequestException as e:
        # API returned error (like 404 for missing group)
        print(f"[WARN] Failed to fetch Team Admin emails for project '{project}': {e}. Using default email.")
        return [default_email]
    except Exception as e:
        # Any other errors
        print(f"[WARN] Unexpected error fetching Team Admin emails for project '{project}': {e}. Using default email.")
        return [default_email]
