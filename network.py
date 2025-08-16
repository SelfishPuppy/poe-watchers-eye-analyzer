import aiohttp
import asyncio
import json
import random

from mod_data import MOD_COMBOS, MOD_NAMES

SINGLE_MODS = list(MOD_NAMES.keys())


class PriceFetcherBackend:
    def __init__(self, single_mode=False):
        self.single_mode = single_mode
        self.running = True
        self.paused = False
        self.results = []
        self.proxy_list = []
        self.proxy_index = 0

    async def load_proxies(self):
        try:
            url = "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    text = await resp.text()
                    self.proxy_list = list(set(line.strip() for line in text.splitlines() if line.strip()))
        except Exception as e:
            print("Failed to load proxies:", str(e))
            self.proxy_list = []

    def get_next_proxy(self):
        if not self.proxy_list:
            return None
        self.proxy_index = (self.proxy_index + 1) % len(self.proxy_list)
        return f"http://{self.proxy_list[self.proxy_index]}"

    async def run(self, on_result=None, on_status=None, on_debug=None, on_countdown=None):
        await self.load_proxies()
        headers = {
            "User-Agent": "poe-watchers-eye-analyzer/1.0",
            "Content-Type": "application/json"
        }

        mod_list = SINGLE_MODS if self.single_mode else MOD_COMBOS

        async with aiohttp.ClientSession(headers=headers) as session:
            for mod in mod_list:
                if not self.running:
                    break
                while self.paused:
                    await asyncio.sleep(1)

                mod1, mod2 = (mod, None) if self.single_mode else mod
                mod_label = MOD_NAMES[mod1] if not mod2 else f"{MOD_NAMES[mod1]} + {MOD_NAMES[mod2]}"
                if on_status:
                    on_status(f"Fetching: {mod_label}")

                price = await self.fetch_price(session, mod1, mod2, on_debug)
                if on_result:
                    on_result(price or 0.0, MOD_NAMES[mod1], MOD_NAMES[mod2] if mod2 else "-")

                self.results.append({
                    "mod1": MOD_NAMES[mod1],
                    "mod2": MOD_NAMES[mod2] if mod2 else None,
                    "avg_price": round(price or 0.0, 2)
                })

                await self.save_results()
                for remaining in range(10, 0, -1):
                    if on_countdown:
                        on_countdown(remaining)
                    await asyncio.sleep(1)
                if on_countdown:
                    on_countdown(0)

    async def fetch_price(self, session, mod1, mod2, on_debug):
        payload = {
            "query": {
                "status": {"option": "online"},
                "stats": [{
                    "type": "and",
                    "filters": [{"id": mod1, "disabled": False}] if not mod2 else [
                        {"id": mod1, "disabled": False},
                        {"id": mod2, "disabled": False}
                    ]
                }],
                "filters": {
                    "misc_filters": {
                        "disabled": False,
                        "filters": {
                            "corrupted": {"option": "false"},
                            "ilvl": {"min": 86}
                        }
                    },
                    "trade_filters": {
                        "disabled": False,
                        "filters": {
                            "price": {"option": "divine"}
                        }
                    }
                }
            },
            "sort": {"price": "asc"}
        }

        search_url = "https://www.pathofexile.com/api/trade/search/Mercenaries"
        fetch_url_base = "https://www.pathofexile.com/api/trade/fetch/"

        proxy = self.get_next_proxy()

        try:
            async with session.post(search_url, json=payload, proxy=proxy) as r:
                if r.status != 200:
                    if on_debug:
                        on_debug(f"Search Error: {r.status}")
                    return None
                search_data = await r.json()

            if not search_data.get("result"):
                return None

            ids = search_data["result"][:5]
            fetch_url = f"{fetch_url_base}{','.join(ids)}?query={search_data['id']}"

            async with session.get(fetch_url, proxy=proxy) as r:
                if r.status != 200:
                    if on_debug:
                        on_debug(f"Fetch Error: {r.status}")
                    return None
                data = await r.json()

            prices = []
            for item in data.get("result", []):
                try:
                    price = item["listing"]["price"]
                    if price["currency"] == "divine":
                        prices.append(price["amount"])
                except KeyError:
                    continue

            return sum(prices) / len(prices) if prices else None

        except Exception as e:
            if on_debug:
                on_debug(f"Exception: {str(e)}")
            return None

    async def save_results(self):
        try:
            with open("watcher_prices.json", "w", encoding="utf-8") as f:
                json.dump(self.results, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("File write error:", str(e))

    def stop(self):
        self.running = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
