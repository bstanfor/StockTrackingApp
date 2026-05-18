StockTrackingApp
================

This is a simple Flask-based stock transaction tracker using SQLite.

Project structure:
- app/main.py      : Flask application and database logic
- app/templates/   : Jinja2 templates for index and edit pages
- app/finance.db   : SQLite database file used by the app
- app/test_app.py  : simple integration test for the index page
- docker-compose.yml and Dockerfile : optional container deployment

How to run locally:
1. Open a terminal in the repository root: `C:\Users\UserName\StockTrackingApp`
2. Activate the virtual environment:
   - PowerShell: `C:\Users\UserName\StockTrackingApp\venv\Scripts\Activate.ps1`
3. Run the Flask app:
   - `python app/main.py`
4. Open the browser at `http://localhost:5000`

Notes:
- The app uses `get_db_connection()` for SQLite access and `sqlite3.Row` to support named-column access in templates.
- Recent changes replaced tuple-index DB reads with named-field access to prevent column mapping errors.
- The project has a local git commit for these updates.

Testing:
- Run `python app/test_app.py` to verify the index page renders and sample data is visible.
