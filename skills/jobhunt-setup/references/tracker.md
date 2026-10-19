# Tracker

The tracker is a view of the local database. Default: `~/.jobhunt/tracker.xlsx`.

Google Sheets instead:

1. The user creates a Google Cloud service account, downloads its JSON key into
   `~/.jobhunt/`, and shares their sheet with the service account's email (Editor).
2. In `~/.jobhunt/config.yaml`:

   ```yaml
   tracker:
     kind: sheets
     sheets:
       spreadsheet_id: <the id in the sheet's URL>
       service_account_file: <path to the JSON key>
   ```

3. `jobhunt doctor` checks it; `jobhunt sync` fills it.

The user should create the service account themselves; never handle their Google credentials.
While the XLSX is open in Excel or Numbers, sync skips it and says so.
