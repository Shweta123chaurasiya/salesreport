# UP Mandi Grocery Price Alerts

A small web app that displays the latest available Uttar Pradesh mandi modal
prices for selected grocery staples and stores WhatsApp daily-digest
subscriptions. Mandi prices are wholesale rates, not retail shop prices.

## Run locally

1. Use Python 3.9 or newer and install dependencies:

   ```sh
   python -m pip install -r requirements.txt
   ```

2. Set the data.gov.in API key and run the first price refresh:

   ```sh
   export DATA_GOV_API_KEY="your-data-gov-api-key"
   python grocery_app.py refresh
   ```

3. Start the web app and open <http://127.0.0.1:5000>:

   ```sh
   python grocery_app.py serve
   ```

## Enable WhatsApp digests

Configure WhatsApp Business Cloud API credentials as environment variables:

- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_API_VERSION`
- `WHATSAPP_TEMPLATE_NAME`
- `WHATSAPP_TEMPLATE_LANGUAGE` (optional; defaults to `en_US`)

Create and get approval for a WhatsApp message template whose body contains
two text parameters, for example: `UP mandi prices for {{1}}: {{2}}`. The app
passes the selected district as `{{1}}` and the daily price digest as `{{2}}`.
Recipients must explicitly opt in through the form.

Run `python grocery_app.py refresh` once each day to fetch new rates and send
the digest. For automation, schedule that command with the scheduler on the
host that runs the app (for example, cron on Linux/macOS). Do not expose the
Flask development server directly to the public internet; deploy behind a
production WSGI server and HTTPS before accepting real phone numbers.

The first version includes onion, potato, tomato, rice, wheat, and garlic, and
defaults to Chandauli district. Rates come from the data.gov.in mandi feed,
are converted from Rs/quintal to Rs/kg, and are shown with the mandi and
reported date. Prices are limited to records returned by that feed; this is
not a live retail-store comparison.
