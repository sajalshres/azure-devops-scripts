async def get_team_admin_emails(self, project, default_email="devops@firstcitizens.com"):
    try:
        url = f"{self.org_url}/{project}/_apis/graph/groups?scopeDescriptor=Project&api-version={self.api_version}"
        try:
            groups = await self._request("GET", url)
        except AzureDevOpsRequestException as e:
            if e.response_status_code == 404:
                print(f"[INFO] Graph API not supported for project '{project}', using default email")
                return [default_email]
            raise e

        if not groups or "value" not in groups:
            print(f"[WARN] No group data for project '{project}', using default email")
            return [default_email]

        team_admin = next((g for g in groups.get("value", []) if "Team Admin" in g.get("displayName", "")), None)
        if not team_admin:
            print(f"No Team Admin group found for project '{project}', using default email")
            return [default_email]

        url_members = f"{self.org_url}/_apis/graph/groups/{team_admin['descriptor']}/members?api-version={self.api_version}"
        members_data = await self._request("GET", url_members)
        if not members_data or "value" not in members_data:
            print(f"[WARN] No members found in Team Admin for project '{project}', using default email")
            return [default_email]

        emails = [m["principalName"] for m in members_data.get("value", []) if m.get("principalName")]
        return emails or [default_email]

    except Exception as e:
        print(f"[WARN] Unexpected error fetching Team Admin for project '{project}': {e}. Using default email.")
        return [default_email]
