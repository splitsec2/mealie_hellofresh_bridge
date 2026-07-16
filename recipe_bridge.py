import datetime
import json
import logging
import os
from argparse import ArgumentParser

import requests


class HttpResponder:
    def __init__(self) -> None:
        pass

    def json_request(self, url, method, headers=None, params=None, data=None):
        try:
            if method == "get":
                with requests.get(
                    url=url, headers=headers, params=params
                ) as r:
                    r.raise_for_status()
                    return r.json()
            elif method == "post":
                with requests.post(
                    url=url, headers=headers, params=params, json=data
                ) as r:
                    r.raise_for_status()
                    return r.json()
            elif method == "patch":
                with requests.patch(
                    url=url, headers=headers, params=params, json=data
                ) as r:
                    r.raise_for_status()
                    return r.json()
            else:
                logging.error(f"Non supported/incorrect HTTP verb: {method}")
                exit(1)
        except requests.RequestException as e:
            logging.error(f"Request error: {e}")
            exit(1)
        except json.JSONDecodeError as e:
            logging.error(f"Error decoding response as JSON: {e}")
            exit(1)


class HelloFresh(HttpResponder):
    def __init__(self, base_url, auth_token, country, language) -> None:
        self.base_url = base_url
        self.auth_token = auth_token
        self.recipes = set()
        self.headers = {
            "authorization": self.auth_token,
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        self.params = {
            "country": country.upper(),
            "locale": f"{country.lower()}-{language.upper()}",
        }

    def set_customer_id(self) -> None:
        customer_res = self.json_request(
            url=f"{self.base_url}/api/customers/me/subscriptions",
            method="get",
            headers=self.headers,
            params=self.params,
        )
        logging.debug(
            f'Adding customer ID {customer_res["items"][0]["id"]} to params'
        )
        self.params["subscription"] = customer_res["items"][0]["id"]

    def set_current_week(self) -> None:
        today = datetime.date.today()
        logging.debug("Setting from HTTP parameter to current date")
        self.params["from"] = f"{today.year}-W{today.strftime('%V')}"

    def add_monthly_recipes(self, deliveries) -> None:
        for weekly_delivery in deliveries["weeks"]:
            meals = weekly_delivery["meals"]
            for meal in meals:
                logging.debug(
                    f'Getting HelloFresh recipe URL: {meal["websiteURL"]}'
                )
                self.recipes.add(meal["websiteURL"])

    def get_past_deliveries(self, additional_deliveries) -> None:
        logging.debug("Getting last month deliveries")
        while True:
            most_recent_deliveries = self.json_request(
                url=f"{self.base_url}/my-deliveries/past-deliveries",
                method="get",
                headers=self.headers,
                params=self.params,
            )
            self.add_monthly_recipes(most_recent_deliveries)
            if additional_deliveries > 0:
                logging.debug("Getting more previous deliveries")
                try:
                    self.params["from"] = most_recent_deliveries["nextWeek"]
                except KeyError:
                    logging.error(
                        f"Asked to retrieve {additional_deliveries} more months but no more deliveries found"
                    )
                    return
                additional_deliveries -= 1
            else:
                return


class Mealie(HttpResponder):
    def __init__(self, base_url, auth_token) -> None:
        self.base_url = base_url
        self.auth_token = auth_token
        self.headers = {
            "Authorization": self.auth_token,
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        self.tagged_recipes = set()

    def create_tag(self, tag) -> None:
        self.tag = self.json_request(
            url=f"{self.base_url}/api/organizers/tags",
            method="post",
            headers=self.headers,
            data={"name": tag},
        )
        logging.debug(f"Tag {tag} has been created")

    def set_tag_id(self, tag) -> None:
        logging.debug(f"Getting {tag} tag infos")
        tag_id_res = self.json_request(
            url=f"{self.base_url}/api/organizers/tags",
            method="get",
            headers=self.headers,
            params={"search": tag},
        )
        if not tag_id_res["items"]:
            logging.info(f"Tag {tag} doesn't exist in Mealie, creating it")
            self.create_tag(tag)
        else:
            self.tag = tag_id_res["items"][0]

    def get_tagged_recipes(self, tag) -> None:
        self.set_tag_id(tag)
        # Retrieve recipes count to infer paging
        tagged_recipes_nb_res = self.json_request(
            url=f"{self.base_url}/api/recipes",
            method="get",
            headers=self.headers,
            params={"tags": self.tag, "perPage": 0},
        )
        tagged_recipes_nb = tagged_recipes_nb_res["total"]

        tagged_recipes_res = self.json_request(
            url=f"{self.base_url}/api/recipes",
            method="get",
            headers=self.headers,
            params={"tags": self.tag, "perPage": tagged_recipes_nb},
        )
        self.tagged_recipes = {
            recipe["orgURL"] for recipe in tagged_recipes_res["items"]
        }

    def add_mealie_recipe(self, recipe_url):
        logging.info(f"Creating new recipe with url: {recipe_url}")
        return(self.json_request(
            url=f"{self.base_url}/api/recipes/create/url",
            method="post",
            headers=self.headers,
            data={"url": recipe_url},
        ))

    def get_mealie_recipe(self, recipe_slug) -> None:
        logging.debug("Getting newly created recipe ID")
        return(self.json_request(
            url=f"{self.base_url}/api/recipes/{recipe_slug}",
            method="get",
            headers=self.headers,
        ))

    def update_mealie_recipe(self, recipe_slug) -> None:
        recipe_body = self.get_mealie_recipe(recipe_slug)
        recipe_body["tags"].append(self.tag)
        logging.debug("Patching recipe to add custom tag to it")
        _ = self.json_request(
            url=f"{self.base_url}/api/recipes/{recipe_slug}",
            method="patch",
            headers=self.headers,
            data=recipe_body,
        )


def load_exclusions(path):
    """Recipe URLs to never import, one per line (# comments and blanks ignored).

    Lets you delete a recipe you disliked from Mealie without it being
    re-imported on the next run.
    """
    excluded = set()
    if not path:
        return excluded
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                excluded.add(line)
    return excluded


def main():
    hellofresh_token = os.environ.get("hellofresh_token")
    if hellofresh_token == None:
        logging.error("Could not load required env var: HELLOFRESH_TOKEN")
        exit(1)
    mealie_token = os.environ.get("mealie_token")
    if mealie_token == None:
        logging.error("Could not load required env var: MEALIE_TOKEN")
        exit(1)

    eligible_countries = [
        "at",
        "ch",
        "fr",
        "lu",
        "au",
        "de",
        "gb",
        "nl",
        "se",
        "be",
        "dk",
        "ie",
        "no",
        "us",
        "ca",
        "es",
        "it",
        "nz",
    ]
    eligible_languages = ["de", "fr", "en", "nl", "sv", "da", "nb", "es", "it"]
    argParser = ArgumentParser()
    argParser.add_argument(
        "--country",
        "-c",
        help="Country linked to your HelloFresh account",
        choices=eligible_countries,
        required=True,
    )
    argParser.add_argument(
        "--language",
        "-l",
        help="Locale linked to your HelloFresh account",
        choices=eligible_languages,
        required=True,
    )
    argParser.add_argument(
        "--mealie-tag",
        "-t",
        help="Mealie tag to group HelloFresh recipes (default: HelloFresh)",
        default="HelloFresh",
    )
    argParser.add_argument(
        "--additional-deliveries",
        "-a",
        help="Number of additional months to retrieve from HelloFresh",
        type=int,
        default=0,
    )
    argParser.add_argument(
        "--exclude-file",
        "-e",
        help="File of recipe URLs to never import, one per line (# comments ok). Lets you delete a recipe from Mealie without it re-importing.",
        default=None,
    )
    argParser.add_argument(
        "--debug", help="Enable debug logs", action="store_true"
    )
    argParser.add_argument(
        "--dry-run",
        "-d",
        help="Just fetch and count recipes from HelloFresh that would be added to Mealie",
        action="store_true",
    )
    args = argParser.parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    hellofresh_api_url = f"https://www.hellofresh.{args.country.lower()}/gw"
    hellofresh_client = HelloFresh(
        hellofresh_api_url, hellofresh_token, args.country, args.language
    )
    hellofresh_client.set_customer_id()
    hellofresh_client.set_current_week()
    hellofresh_client.get_past_deliveries(args.additional_deliveries)
    logging.info(
        f"Scrapped {len(hellofresh_client.recipes)} HelloFresh recipes"
    )

    mealie_api_url = "https://food.syyrell.com"
    mealie_client = Mealie(mealie_api_url, mealie_token)
    mealie_client.get_tagged_recipes(args.mealie_tag)
    excluded = load_exclusions(args.exclude_file)
    if excluded:
        logging.info(
            f"{len(excluded)} recipe(s) from exclude file will be skipped"
        )
    already_present = mealie_client.tagged_recipes | excluded
    if args.dry_run:
        new_recipes = hellofresh_client.recipes - already_present
        if len(new_recipes) > 0:
            logging.info(
                f"Would have added {len(new_recipes)} recipes to Mealie:"
            )
            {logging.info(recipe) for recipe in new_recipes}
        else:
            logging.info("All fetched recipes already exist in Mealie!")
        exit(0)
    new_recipes = hellofresh_client.recipes - already_present
    if not new_recipes:
        logging.info("All scrapped recipes already in Mealie, exiting.")
        exit(0)
    for new_recipe in new_recipes:
        recipe_slug = mealie_client.add_mealie_recipe(new_recipe)
        mealie_client.update_mealie_recipe(recipe_slug)


if __name__ == "__main__":
    main()
