"""Script to check Azure DevOps Pipeline inactivity and disable them."""

import argparse
import asyncio
import base64
import logging
import os
from datetime import datetime, timedelta
from json import JSONDecodeError
from typing import Any, Dict, List

import aiohttp
import aiohttp.client_exceptions
import requests

logger = logging.getLogger(__name__)

LOG_LEVEL = logging.INFO

if os.getenv("DEBUG", "false").lower() == "true":
    LOG_LEVEL = logging.DEBUG

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%m-%d %H:%M",
    level=LOG_LEVEL,
)

if os.getenv("ENVIRONMENT", "PRODUCTION") == "DEVELOP":
    import dotenv

    dotenv.load_dotenv(os.getenv("DOTENV_FILE", "../.env"), override=True)

semaphore = asyncio.Semaphore(10)


def str_to_bool(value: str) -> bool:
    """Converts string to a boolean value"""
    return value.lower() in ["true", "1", "t", "y", "yes"]


def get_argument_parser() -> argparse.ArgumentParser:
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(
        usage="%(prog)s [OPTIONS]",
        description="Check for inactive Azure DevOps pipelines and optionally disable them.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("AZDO_HOST"),
        dest="azdo_host",
        help="Azure DevOps Host or Domain.",
    )
    parser.add_argument(
        "--organization",
        default=os.getenv("AZDO_ORGANIZATION"),
        dest="azdo_organization",
        help="Azure DevOps Organization.",
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
        help="Azure DevOps Personal Access Token.",
    )

    return parser


class AzDoMultipleFolderException(Exception):
    """Raised when multiple folders are found"""

    pass


class AzDoSession:
    """Azure DevOps Session"""

    def __init__(
        self,
        host: str,
        organization: str,
        pat: str,
        verify: bool = False,
        api_version: str = "7.1-preview",
    ) -> None:
        self.host = host
        self.organization = organization
        self._pat = pat
        self.base_url = f"https://{host}/{organization}"
        self.headers = {
            "Authorization": "Basic "
            + str(base64.b64encode(bytes(f":{self._pat}", "ascii")), "ascii"),
            "Content-Type": "application/json",
        }
        self.api_version = api_version

        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(verify_ssl=verify), headers=self.headers
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, exc_traceback):
        if exc_type:
            logger.exception(exc_type)
            logger.exception(exc_value)
            logger.exception(exc_traceback)
        await self.session.close()

    async def get(self, url: str, params: dict = None) -> Any:
        """Get method wrapper"""

        if params is None:
            params = {}

        async with self.session.get(
            url, params=params, raise_for_status=True
        ) as response:
            try:
                return await response.json()
            except JSONDecodeError as error:
                logger.warning("Failed to decode json response %s", error)
                return await response.text()

    async def put(self, url: str, json: dict, params: dict = None) -> Any:
        """Put method wrapper"""

        if params is None:
            params = {}

        try:
            async with self.session.put(
                url, json=json, params=params, raise_for_status=True
            ) as response:
                try:
                    return await response.json()
                except JSONDecodeError as error:
                    logger.warning("Failed to decode json response %s", error)
                    return await response.text()
        except aiohttp.client_exceptions.ClientResponseError as error:
            logger.error(error)
        except Exception as error:
            logger.error("Unexpected error: %s", error)

    async def get_projects(self) -> list:
        """fetches a list of projects within organization"""
        response = await self.get(f"{self.base_url}/_apis/projects")
        return response.get("value", [])

    async def get_pipelines(self, project_name: str) -> list:
        """fetches a list of pipelines within organization"""
        response = await self.get(
            f"{self.base_url}/{project_name}/_apis/build/definitions"
        )
        return response.get("value", [])

    async def get_pipeline_builds(self, project_name: str, params: dict = None) -> list:
        """fetches a list of pipelines within organization"""
        response = await self.get(
            f"{self.base_url}/{project_name}/_apis/build/builds", params=params
        )
        return response.get("value", [])

    async def get_pipeline_build_definition(
        self, project_name: str, pipeline_id: str
    ) -> list:
        """fetches a list of pipelines within organization"""
        response = await self.get(
            f"{self.base_url}/{project_name}/_apis/build/definitions/{pipeline_id}"
        )
        return response

    async def create_folder(self, project_name: str, folder_name: str) -> list:
        """Create a folder in pipeline"""
        url = f"{self.base_url}/{project_name}/_apis/build/folders"
        params = {"path": f"\\{folder_name}"}

        folder = await self.put(url, json={"path": f"\\{folder_name}"}, params=params)
        return folder

    async def get_or_create_folder(self, project_name: str, folder_name: str) -> list:
        """Get folder if it exists otherwise create a folder"""
        url = f"{self.base_url}/{project_name}/_apis/build/folders"
        params = {"path": f"\\{folder_name}"}

        folder_response = await self.get(url, params=params)
        folder = folder_response.get("value", [])

        if folder_response["count"] > 1:
            raise AzDoMultipleFolderException(
                f"Multiple folder with name {folder_name} found."
            )

        # create folder if it doenot exist
        if not folder:
            folder = await self.put(
                url, json={"path": f"\\{folder_name}"}, params=params
            )
        else:
            folder = folder[0]

        return folder

    async def disable_and_archive_pipeline(
        self, project_name: str, pipeline_id: str, archive_folder_name: str = "archive"
    ) -> dict:
        """Disables and archives the pipeline."""
        async with semaphore:
            pipeline_definition = await self.get_pipeline_build_definition(
                project_name=project_name, pipeline_id=pipeline_id
            )
            pipeline_definition["queueStatus"] = "disabled"
            pipeline_definition["path"] = f"\\{archive_folder_name}"

            response = await self.put(
                f"{self.base_url}/{project_name}/_apis/build/definitions/{pipeline_id}",
                json=pipeline_definition,
            )
            return response


async def find_inactive_pipeline(
    session: AzDoSession, project_name: str, threshold_days: str = 365
) -> list:
    """identiy a list of inactive pipelines"""
    threshold_days = datetime.now() - timedelta(days=threshold_days)

    async with semaphore:
        pipelines = await session.get_pipelines(project_name=project_name)

        # result
        inactive_pipelines = []

        for pipeline in pipelines:
            # parse only first 18 digits of datetime due to inconsistencies in Azure DevOps APIs
            created_date = datetime.strptime(
                pipeline["createdDate"][:19], "%Y-%m-%dT%H:%M:%S"
            )

            if created_date < threshold_days:
                logger.debug(
                    "%s pipeline is inactive since %s", pipeline["name"], created_date
                )
                build_response = await session.get_pipeline_builds(
                    project_name=project_name,
                    params={"definitions": pipeline["id"], "$top": 1},
                )

                if len(build_response) != 0:
                    continue

                inactive_pipelines.append((project_name, pipeline["id"]))

        logger.info(
            "--> %d inactive pipelines in %s project.",
            len(inactive_pipelines),
            project_name,
        )

        return inactive_pipelines


async def main() -> None:
    """main function"""

    # parse arguments
    parser = get_argument_parser()
    args = parser.parse_args()

    # set variables
    azdo_host = args.azdo_host
    azdo_organization = args.azdo_organization
    azdo_pat = args.azdo_pat
    dry_run = args.dry_run

    async with AzDoSession(
        host=azdo_host, organization=azdo_organization, pat=azdo_pat, verify=True
    ) as session:
        # get projects
        projects = await session.get_projects()

        logger.info("Found %d projects in %s", len(projects), azdo_organization)

        # identify inactive pipelines
        tasks = [
            find_inactive_pipeline(session, project_name=project["name"])
            for project in projects
        ]
        results = await asyncio.gather(*tasks)
        all_inactive_pipelines = [item for sublist in results for item in sublist]

        logger.info(
            "%d pipelines are inactive in %s organization",
            len(all_inactive_pipelines),
            azdo_organization,
        )

        # disable pipeline
        if not dry_run:
            logger.info("diabling pipelines")

            # create archive folder first:
            unique_project_names = {item[0] for item in all_inactive_pipelines}
            for project_name in unique_project_names:
                logger.info("--> creating archive folder in %s project", project_name)
                async with asyncio.Semaphore(1):
                    _ = await session.get_or_create_folder(
                        project_name=project_name, folder_name="archive"
                    )

            # disable
            disable_tasks = [
                session.disable_and_archive_pipeline(
                    project_name, pipeline_id=pipeline_id
                )
                for project_name, pipeline_id in all_inactive_pipelines
            ]

            disable_results = await asyncio.gather(*disable_tasks)
            logger.info("%d pipeline successfully disabled", len(disable_results))


if __name__ == "__main__":
    asyncio.run(main())
