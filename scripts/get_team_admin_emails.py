# Get Team Admin emails
async def get_team_admin_emails(
    self, project, default_email="devops@firstcitizens.com"
):
    try:
        # 1. Get groups in the project
        url = f"{self.org_url}/{project}/_apis/graph/groups?scopeDescriptor=Project&api-version={self.api_version}"
        groups = await self._request("GET", url)
        if not groups or "value" not in groups:
            print(f"[WARN] No group data for project '{project}', using default email")
            return [default_email]

        # 2. Find Team Admin group
        team_admin = next(
            (g for g in groups.get("value", []) if "Team Admin" in g.get("displayName", "")),
            None,
        )
        if not team_admin:
            print(f"No Team Admin group found for project '{project}', using default email")
            return [default_email]

        # 3. Get members of Team Admin group
        url_members = f"{self.org_url}/_apis/graph/groups/{team_admin['descriptor']}/members?api-version={self.api_version}"
        members_data = await self._request("GET", url_members)
        if not members_data or "value" not in members_data:
            print(f"[WARN] No members found in Team Admin for project '{project}', using default email")
            return [default_email]

        emails = []
        for member in members_data.get("value", []):
            if member.get("principalName"):
                emails.append(member["principalName"])

        if not emails:
            return [default_email]

        return emails

    except AzureDevOpsRequestException as e:
        print(f"[WARN] Azure DevOps API error fetching Team Admin for project '{project}': {e}. Using default email.")
        return [default_email]

    except Exception as e:
        print(f"[WARN] Unexpected error fetching Team Admin for project '{project}': {e}. Using default email.")
        return [default_email]







async def _request(self, method, url, json_data=None):
    async with self.session.request(method, url, json=json_data) as response:
        text = await response.text()
        if response.status >= 400:
            raise AzureDevOpsRequestException(
                request_method=method,
                request_url=url,
                response_status_code=response.status,
                response_text=text,
                message=f"{method} {url} failed: {response.status} - {text}",
            )
        # Try to parse JSON, fallback to None if invalid
        try:
            return await response.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError):
            print(f"[WARN] Response from {url} is not JSON, content: {text[:200]}")
            return None
