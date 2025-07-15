
## 📄 `main.py`

import sys
import asyncio
import aiohttp
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QLabel
)
from mod_data import MOD_COMBOS, MOD_NAMES

class PriceFetcher(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PoE Watcher's Eye Analyzer")
        self.resize(600, 400)
        layout = QVBoxLayout()

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_data)
        layout.addWidget(self.refresh_button)

        self.status_label = QLabel("Ready.")
        layout.addWidget(self.status_label)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Mod 1", "Mod 2", "Avg Price (Divine)"])
        layout.addWidget(self.table)

        self.setLayout(layout)

    def refresh_data(self):
        self.status_label.setText("Querying trade site...")
        asyncio.create_task(self.run_price_checks())

    async def run_price_checks(self):
        self.table.setRowCount(0)
        async with aiohttp.ClientSession() as session:
            for mod1, mod2 in MOD_COMBOS:
                avg_price = await self.query_price(session, mod1, mod2)
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem(MOD_NAMES[mod1]))
                self.table.setItem(row, 1, QTableWidgetItem(MOD_NAMES[mod2]))
                self.table.setItem(row, 2, QTableWidgetItem(f"{avg_price:.2f}" if avg_price else "N/A"))
        self.status_label.setText("Done.")

    async def query_price(self, session, mod1, mod2):
        payload = {
            "query": {
                "status": {"option": "online"},
                "name": "Watcher's Eye",
                "type": "Watcher's Eye",
                "stats": [{
                    "type": "and",
                    "filters": [{"id": mod1, "disabled": False}, {"id": mod2, "disabled": False}]
                }]
            },
            "sort": {"price": "asc"}
        }
        search_url = "https://www.pathofexile.com/api/trade/search/Mercenaries"
        async with session.post(search_url, json=payload) as r:
            data = await r.json()
        if not data.get("result"):
            return None
        result_ids = ",".join(data["result"][:5])
        fetch_url = f"https://www.pathofexile.com/api/trade/fetch/{result_ids}?query={data['id']}"
        async with session.get(fetch_url) as r:
            listings = await r.json()
        prices = []
        for item in listings.get("result", []):
            price = item["listing"]["price"]
            if price["currency"] == "divine":
                prices.append(price["amount"])
        if prices:
            return sum(prices) / len(prices)
        return None

def main():
    app = QApplication(sys.argv)
    loop = asyncio.get_event_loop()
    fetcher = PriceFetcher()
    fetcher.show()
    loop.run_until_complete(app.exec_())

if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # Windows fix
    main()
