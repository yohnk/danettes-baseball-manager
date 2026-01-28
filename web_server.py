import asyncio
import datetime
from typing import Optional

from constants import WEB_DIRECTORY
from yahoo_api import *
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

class WebServer:
    def __init__(self, yahoo_api: YahooApiManager):
        self.app = FastAPI()
        self.yahoo_api = yahoo_api
        static_files = StaticFiles(directory=WEB_DIRECTORY, html=True)

        self.app.mount("/web", static_files, name="web")

    def _dataframe_to_table(self, df: pd.DataFrame) -> List[Dict[str, str]]:
        return [row.to_dict() for _, row in df.iterrows()]

    async def serve(self):
        app: FastAPI = self.app

        @app.get("/", include_in_schema=False)
        async def _get_root(request: Request):
            return HTMLResponse(f"{request.client.host}")

        @app.get("/health-check", include_in_schema=False)
        @app.head("/health-check", include_in_schema=False)
        async def _get_health_check(request: Request):
            return HTMLResponse(f"OK")

        @app.get("/draft-cost", include_in_schema=False)
        async def _get_draft_cost(request: Request):
            return JSONResponse(content=self._dataframe_to_table(self.yahoo_api.draft_costs), media_type="application/json")

        @app.get("/draft-cost-settings", include_in_schema=False)
        async def _get_draft_cost_settings(request: Request):
            settings = {
                "ajaxURL": "/draft-cost",
                # Use the order they are in the dataframe
                "columns":[{"title":str(column), "field":str(column), "headerFilter": "input" if (column == "Player" or column == "Team") else str(False)} for column in self.yahoo_api.draft_costs.columns],
                # "autoColumns": True
                "pagination": True,
                "paginationMode": "local",
                "paginationSize": 100,
                "initialSort": [{"column":str(datetime.datetime.now().year), "dir":"asc"}]
            }

            return JSONResponse(content=settings, media_type="application/json")

        @app.get("/draft-history", include_in_schema=False)
        async def _get_draft_history(request: Request):
            output = []
            for draft in self.yahoo_api.draft_results:
                for pick in draft.picks:
                    output.append({"Season":draft.season, "Pick": pick.pick, "Round": pick.round, "Player": pick.player.name, "Team": pick.team.name, "Keeper": pick.keeper})
            return JSONResponse(content=output, media_type="application/json")

        @app.get("/draft-history-settings", include_in_schema=False)
        async def _get_draft_history_settings(request: Request):
            settings = {
                "ajaxURL": "/draft-history",
                # Use the order they are in the dataframe
                "columns": [
                    {"title": "Season", "field": "Season", "headerFilter": "input", "sorter":"alphanum"},
                    {"title": "Pick", "field": "Pick", "headerFilter": "input", "sorter":"alphanum"},
                    {"title": "Round", "field": "Round", "headerFilter": "input", "sorter":"alphanum"},
                    {"title": "Player", "field": "Player", "headerFilter": "input"},
                    {"title": "Team", "field": "Team", "headerFilter": "input"},
                    {"title": "Keeper", "field": "Keeper", "headerFilter": "input"},
                ],
                # "autoColumns": True
                "pagination": True,
                "paginationMode": "local",
                "paginationSize": 100,
                "initialSort": [
                    {"column": "Pick", "dir": "desc"},
                    {"column": "Season", "dir": "desc"},
                ]
            }

            return JSONResponse(content=settings, media_type="application/json")

        # serve
        config = uvicorn.Config(app, host="0.0.0.0", port=8000)
        server = uvicorn.Server(config)
        await server.serve()


if __name__ == "__main__":
    api = None
    try:
        api = YahooApiManager(run=True)
        instance = WebServer(api)
        asyncio.run(instance.serve())
    finally:
        if api is not None:
            api.kill()