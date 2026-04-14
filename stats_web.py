#!/usr/bin/env python3
import html
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


DB_PATH = os.getenv("WEATHER_DB_PATH", "weather_bot.db")
HOST = os.getenv("STATS_HOST", "0.0.0.0")
PORT = int(os.getenv("STATS_PORT", "8081"))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_overview() -> dict:
    with get_connection() as conn:
        users = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
        favorites = conn.execute("SELECT COUNT(*) AS cnt FROM favorite_routes").fetchone()["cnt"]
        top = conn.execute(
            """
            SELECT fr.name, u.telegram_user_id, fr.times_used, fr.source_type
            FROM favorite_routes fr
            JOIN users u ON u.id = fr.user_id
            ORDER BY fr.times_used DESC, fr.updated_at DESC
            LIMIT 20
            """
        ).fetchall()
    return {"users": users, "favorites": favorites, "top_routes": top}


def fetch_user_favorites(telegram_user_id: int):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT fr.id, fr.name, fr.source_type, fr.times_used, fr.created_at, fr.updated_at
            FROM favorite_routes fr
            JOIN users u ON u.id = fr.user_id
            WHERE u.telegram_user_id = ?
            ORDER BY fr.updated_at DESC, fr.id DESC
            """,
            (telegram_user_id,),
        ).fetchall()
    return rows


def render_page(query: dict) -> str:
    overview = fetch_overview()
    user_filter = (query.get("user_id") or [""])[0].strip()
    filtered_rows = []
    filter_error = ""

    if user_filter:
        try:
            filtered_rows = fetch_user_favorites(int(user_filter))
        except ValueError:
            filter_error = "user_id must be an integer"

    rows_html = ""
    for row in overview["top_routes"]:
        rows_html += (
            "<tr>"
            f"<td>{html.escape(str(row['telegram_user_id']))}</td>"
            f"<td>{html.escape(row['name'])}</td>"
            f"<td>{html.escape(row['source_type'])}</td>"
            f"<td>{row['times_used']}</td>"
            "</tr>"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='4'>No data yet</td></tr>"

    filtered_html = ""
    if user_filter:
        if filter_error:
            filtered_html = f"<p style='color:#c00'>{html.escape(filter_error)}</p>"
        else:
            frows = ""
            for row in filtered_rows:
                frows += (
                    "<tr>"
                    f"<td>{row['id']}</td>"
                    f"<td>{html.escape(row['name'])}</td>"
                    f"<td>{html.escape(row['source_type'])}</td>"
                    f"<td>{row['times_used']}</td>"
                    f"<td>{html.escape(row['updated_at'])}</td>"
                    "</tr>"
                )
            if not frows:
                frows = "<tr><td colspan='5'>No favorites for this user</td></tr>"
            filtered_html = (
                "<h3>Favorites for user_id="
                f"{html.escape(user_filter)}</h3>"
                "<table><thead><tr>"
                "<th>ID</th><th>Name</th><th>Source</th><th>Used</th><th>Updated</th>"
                "</tr></thead><tbody>"
                f"{frows}</tbody></table>"
            )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Weather Bot Stats</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #222; }}
    .cards {{ display: flex; gap: 16px; margin-bottom: 20px; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 12px 16px; min-width: 160px; }}
    .n {{ font-size: 24px; font-weight: 700; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
    th, td {{ border: 1px solid #e5e5e5; padding: 8px; text-align: left; }}
    th {{ background: #f8f8f8; }}
    input[type=text] {{ padding: 6px; width: 220px; }}
    button {{ padding: 7px 12px; }}
  </style>
</head>
<body>
  <h2>Weather Bot Favorites Stats</h2>
  <div class="cards">
    <div class="card"><div>Total users</div><div class="n">{overview['users']}</div></div>
    <div class="card"><div>Total favorites</div><div class="n">{overview['favorites']}</div></div>
  </div>

  <form method="get">
    <label>Filter by Telegram user ID:</label><br/>
    <input type="text" name="user_id" value="{html.escape(user_filter)}" />
    <button type="submit">Filter</button>
  </form>
  {filtered_html}

  <h3>Top favorites by usage</h3>
  <table>
    <thead>
      <tr><th>User ID</th><th>Name</th><th>Source</th><th>Times used</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            body = render_page(query).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            msg = f"stats_web error: {exc}".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Stats web started on http://{HOST}:{PORT} (db={DB_PATH})")
    server.serve_forever()
