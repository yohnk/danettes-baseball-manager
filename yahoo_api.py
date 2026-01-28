import datetime
import json
import os
import pickle
import shutil
import time
from threading import Thread
from typing import List, Dict, Tuple
import csv

import logging
from yahoo_fantasy_api import League
from yahoo_oauth import OAuth2
import yahoo_fantasy_api as yfa
from constants import YAHOO_API_CREDENTIALS_PATH, DATA_DIRECTORY, REFRESH_EVERY_SEC, PURGE_EVERY_SEC, LOG_DIRECTORY, START_YEAR
import pandas as pd

#Setup logging
# logging.basicConfig(filename=, encoding="utf-8", filemode="a", format="{asctime}:{levelname}: {message}", style="{", datefmt="%Y-%m-%d %H:%M")

logFormatter = logging.Formatter("%(asctime)s [%(levelname)-5.5s]  %(message)s")
rootLogger = logging.getLogger()
rootLogger.setLevel(logging.DEBUG)

fileHandler = logging.FileHandler("{0}/{1}.log".format(LOG_DIRECTORY, "yahoo-api"))
fileHandler.setFormatter(logFormatter)
rootLogger.addHandler(fileHandler)

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(logFormatter)
rootLogger.addHandler(consoleHandler)

class Team:

    def __init__(self, yahoo_id: str, name: str):
        self.yahoo_id = yahoo_id
        self.name = name

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return self.name

    def json(self):
        return {
            'yahoo_id': self.yahoo_id,
            'name': self.name,
        }

class Player:

    def __init__(self, yahoo_id: str, name: str):
        self.yahoo_id = yahoo_id
        self.name = name

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return self.name

    def json(self):
        return {
            'yahoo_id': self.yahoo_id,
            'name': self.name,
        }


class Pick:

    def __init__(self, round: int, pick: int, team: Team, player: Player, keeper: bool):
        self.round = round
        self.pick = pick
        self.team = team
        self.player = player
        self.keeper = keeper

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return f'Round: {self.round}, Pick: {self.pick}, Team: {self.team}, Player: {self.player}, Keeper: {self.keeper}'

    def json(self):
        return {
            'round': self.round,
            'pick': self.pick,
            'team': self.team.json(),
            'player': self.player.json(),
            'keeper': self.keeper,
        }


class Draft:

    def __init__(self, season: int, yahoo_league_id: str):
        self.season = season
        self.yahoo_league_id = yahoo_league_id
        self.picks: List[Pick] = []

    def add_pick(self, pick: Pick):
        self.picks.append(pick)
        self.picks.sort(key=lambda p: p.pick)

    def __str__(self):
        return self.__repr__()

    def json(self):
        return {'season': self.season, 'yahoo_league_id': self.yahoo_league_id, 'picks': [p.json() for p in self.picks]}

    def get_pick(self, yahoo_id: int) -> Pick | None:
        for p in self.picks:
            if int(p.player.yahoo_id) == int(yahoo_id):
                return p
        return None


class YahooApiManager:

    def __init__(self, run: bool = True):
        # The first time this runs you'll need to log in through a browser to get a verification code.
        self.sc = OAuth2(consumer_key=None, consumer_secret=None, from_file=YAHOO_API_CREDENTIALS_PATH)
        self.game = yfa.Game(self.sc, 'mlb')
        self.current_league = self._get_current_league()
        self.draft_results: List[Draft] | None = None

        self.draft_costs: pd.DataFrame | None = None

        self._kill_timeout = 10 # sec

        if not os.path.exists(DATA_DIRECTORY):
            os.makedirs(DATA_DIRECTORY, exist_ok=True)

        self.alive = run
        if run:
            self.thread = Thread(target=self.run_loop)
            self.thread.start()

    def kill(self):
        logging.info("Killing ApiManager")
        self.alive = False
        if self.thread is not None:
            self.thread.join(timeout=self._kill_timeout)

    @staticmethod
    def _read_timestamp(directory: str) -> int:
        file_path = os.path.join(directory, 'timestamp')
        if os.path.exists(directory) and os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    return int(f.read())
            except Exception as e:
                logging.error(f'Error reading {file_path}: {e}', exc_info=e)
        return -1

    def run_loop(self):
        while self.alive:
            epoch_sec = int(time.time())
            try:
                runs = [run_dir for run_dir in os.listdir(DATA_DIRECTORY) if run_dir.startswith('run')]
                timestamps = sorted([(run, self._read_timestamp(os.path.join(DATA_DIRECTORY, run))) for run in runs], key=lambda t: t[1], reverse=True)

                if len(timestamps) == 0 or time.time() - timestamps[0][1] > REFRESH_EVERY_SEC:
                    logging.info("Refreshing artifacts")

                    run_directory = os.path.join(DATA_DIRECTORY, f"run-{epoch_sec}")
                    os.makedirs(run_directory, exist_ok=True)

                    self.draft_results = self.create_artifacts(run_directory)
                    self.draft_costs = self.calculate_draft_cost(run_directory)

                    with open(os.path.join(run_directory, "timestamp"), "w") as f:
                        f.write(str(epoch_sec))
                elif len(timestamps) > 0 and timestamps[0][1] > 0 and (self.draft_costs is None or self.draft_results is None):
                    run_directory = os.path.join(DATA_DIRECTORY, timestamps[0][0])

                    with open(os.path.join(run_directory, "draft-results.pkl"), "rb") as draft_results_file:
                        self.draft_results = pickle.load(draft_results_file)

                    with open(os.path.join(run_directory, "draft-cost.pkl"), "rb") as draft_cost_pickle:
                        self.draft_costs = pickle.load(draft_cost_pickle)

                for run, timestamp in timestamps:
                    if time.time() - timestamp > PURGE_EVERY_SEC:
                        logging.info(f"Deleting run {run}")
                        shutil.rmtree(os.path.join(DATA_DIRECTORY, run))

                time.sleep(1)
            except Exception as e:
                logging.error(f'Error in run_loop', exc_info=e)

    def create_artifacts(self, run_directory: str) -> List[Draft]:
        logging.info(f"Creating artifacts")

        draft_results = []

        league_ids = self.game.league_ids(game_codes=["mlb"])
        for league_id in league_ids:
            league: League = self.game.to_league(league_id)
            season = league.settings()["season"]
            if START_YEAR is None or int(season) >= START_YEAR:
                logging.info(f"Creating artifacts for league_id: {league_id}: season {season}")

                teams: Dict[str, Team] = dict()
                teams_raw: Dict[str, Dict] = league.teams()
                for team_id in teams_raw.keys():
                    teams[team_id] = Team(yahoo_id=team_id, name=teams_raw[team_id]["name"])

                draft_results_raw = league.draft_results()
                draft = Draft(season=season, yahoo_league_id=league_id)

                # Fetch all of the players at once to speed up the request
                player_ids = [result["player_id"] for result in draft_results_raw]
                players: Dict[str, Tuple[Player, bool]] = self.get_drafted_player(player_ids, league)

                for result in draft_results_raw:
                    team: Team = teams[result["team_key"]]
                    player, kept = players[str(result["player_id"])]
                    draft.add_pick(Pick(round=result["round"], pick=result["pick"], team=team, player=player, keeper=kept))

                draft_results.append(draft)
                draft_results.sort(key=lambda d: d.season)

        with open(os.path.join(run_directory, "draft-results.json"), "w", encoding='utf-8') as draft_results_file:
            json.dump([d.json() for d in draft_results], draft_results_file, indent=4, ensure_ascii=False)

        with open(os.path.join(run_directory, "draft-results.pkl"), "wb") as draft_results_file:
            pickle.dump(draft_results, draft_results_file)

        with open(os.path.join(run_directory, 'draft-results.csv'), 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Season", "Pick", "Round", "Player", "Player ID", "Team", "Team Id", "Kept"])
            for draft in draft_results:
                for pick in draft.picks:
                    writer.writerow([draft.season, pick.pick, pick.round, pick.player.name, pick.player.yahoo_id, pick.team.name, pick.team.yahoo_id, pick.keeper])

        return draft_results

    def _get_current_league(self, current_year: int = datetime.datetime.now().year) -> League | None:
        league_ids = self.game.league_ids(game_codes=["mlb"])
        for league_id in league_ids:
            league: League = self.game.to_league(league_id)
            season = league.settings()["season"]
            if int(season) == current_year:
                return league
        return None

    def _currently_rostered(self) -> Dict[int, str]:
        output = dict()
        if self.current_league is not None:
            taken_player_ids = [tp["player_id"] for tp in self.current_league.taken_players()]
            # Apparently this method only gives us 25 results max, so do 20 at a time
            for i in range(0, len(taken_player_ids), 20):
                player_slice = taken_player_ids[i:min(i + 20, len(taken_player_ids))]
                ownership = self.current_league.ownership(player_ids=player_slice)
                for player_id in ownership:
                    output[player_id] = ownership[player_id]["owner_team_name"]
        return output


    def calculate_draft_cost(self, run_directory: str, current_year: int = datetime.datetime.now().year) -> pd.DataFrame:
        logging.info(f"Calculating draft cost")
        players: Dict[int, str] = dict()
        ownership: Dict[int, str] = dict()
        for draft in self.draft_results:
            for player in [pick.player for pick in draft.picks]:
                players[player.yahoo_id] = player.name

        currently_rostered = self._currently_rostered()
        for player_id in currently_rostered:
            if player_id not in players:
                details = self.current_league.player_details(player=int(player_id))
                if len(details) > 0:
                    players[player_id] = details[0]["name"]["full"]
            ownership[player_id] = currently_rostered[player_id]



        # Player Id x Year x Cost
        draft_cost: Dict[int, Dict[int, Tuple[int, bool]]] = dict()
        for player_id in players.keys():
            for draft in sorted(self.draft_results, key=lambda d: int(d.season)):
                if player_id not in draft_cost:
                    draft_cost[player_id] = dict()

                pick = draft.get_pick(player_id)
                prev_cost, prev_kept = draft_cost[player_id][int(draft.season) - 1] if int(draft.season) - 1 in draft_cost[player_id] else (25, False)

                is_keeper = ((pick is not None and pick.keeper) or int(draft.season) == current_year)

                if pick is None and not is_keeper:
                    draft_cost[player_id][int(draft.season)] = (25, False)
                # COVID
                elif is_keeper and int(draft.season) == 2021:
                    draft_cost[player_id][int(draft.season)] = (prev_cost, True)
                elif is_keeper and prev_kept:
                    draft_cost[player_id][int(draft.season)] = (prev_cost - 3, True)
                elif is_keeper and (pick is not None or int(draft.season) == current_year):
                    draft_cost[player_id][int(draft.season)] = (prev_cost, True)
                elif is_keeper:
                    draft_cost[player_id][int(draft.season)] = (25, True)
                else:
                    draft_cost[player_id][int(draft.season)] = (pick.round, False)

        header_row = ["ID", "Player", "Team"] + [int(d.season) for d in self.draft_results]
        rows = []
        for player_id in players.keys():
            row = [player_id, players[player_id], ownership[player_id] if player_id in ownership else ""]
            player_results = draft_cost[player_id]
            for season in sorted(player_results.keys()):
                value, kept = player_results[season]
                row.append(value)
            rows.append(row)

        draft_costs = pd.DataFrame(rows, columns=header_row)

        with open(os.path.join(run_directory, "draft-cost.csv"), "w", encoding='utf-8') as draft_cost_csv:
            writer = csv.writer(draft_cost_csv)
            writer.writerow(header_row)
            writer.writerows(rows)

        draft_costs.to_excel(os.path.join(run_directory, "draft-cost.xlsx"))

        with open(os.path.join(run_directory, "draft-cost.pkl"), "wb") as draft_cost_pickle:
            pickle.dump(draft_costs, draft_cost_pickle)

        return draft_costs

    @staticmethod
    def get_drafted_player(player_ids: List[str], league: League) -> Dict[str, Tuple[Player, bool]]:
        players = league.player_details(player_ids)
        output: Dict[str, Tuple[Player, bool]] = dict()
        for player in players:
            output[player["player_id"]] =  Player(yahoo_id=player["player_id"], name=player["name"]["full"]), player["is_keeper"]["kept"]
        return output


if __name__ == "__main__":
    api = YahooApiManager()
    time.sleep(2460)
    api.kill()
