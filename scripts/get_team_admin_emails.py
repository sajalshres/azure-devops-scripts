async def get_team_admin_emails(azdo: AzureDevOpsSession, project: str):
    """
    Fetch Team Admin members emails for a project.
    Fallback to default if no group or members exist.
    """
    DEFAULT_DEVSECOPS_EMAIL = "devsecops@firstcitizens.com"
    team_admin_emails = []

    try:
        # Get all groups in project
        url = f"{azdo.release_url}/{project}/_apis/graph/groups?scopeDescriptor=project:{project}&api-version=7.1-preview.1"
        data = await azdo._request("GET", url)
        groups = data.get("value", [])

        # Find Team Admin group
        team_admin_group = next((g for g in groups if g.get("displayName") == "Team Admin"), None)
        if not team_admin_group:
            print(f"No Team Admin group found for project {project}, will fallback to default email")
            return [DEFAULT_DEVSECOPS_EMAIL]

        # If Team Admin has members
        group_id = team_admin_group["descriptor"]
        url_members = f"{azdo.release_url}/_apis/graph/memberships/{group_id}?api-version=7.1-preview.1"
        members_data = await azdo._request("GET", url_members)
        members = members_data.get("value", [])

        for member in members:
            # Check if it's an AD group or user
            member_url = f"{azdo.release_url}/_apis/graph/users/{member['principalName']}?api-version=7.1-preview.1"
            try:
                user_data = await azdo._request("GET", member_url)
                email = user_data.get("mailAddress")
                if email:
                    team_admin_emails.append(email)
            except AzureDevOpsRequestException:
                # Could be an AD group, try to resolve members if desired
                pass

    except Exception as e:
        print(f"Failed to fetch team admin emails for project {project}: {e}")
        return [DEFAULT_DEVSECOPS_EMAIL]

    if not team_admin_emails:
        print(f"No members found in Team Admin for project {project}, using default email")
        return [DEFAULT_DEVSECOPS_EMAIL]

    return team_admin_emails
