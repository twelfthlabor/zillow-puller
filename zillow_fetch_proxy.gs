/**
 * Zillow fetch proxy — deploy this as a Google Apps Script web app.
 *
 * WHY: Zillow blocks scrapers from flagged/datacenter IPs, but whitelists
 * Google server IP ranges. Fetching Zillow pages through this script returns
 * real 200 pages with __NEXT_DATA__, no captcha, for free.
 *
 * DEPLOY (once, ~5 minutes):
 *   1. Go to https://script.google.com and click "New project".
 *   2. Delete the sample code, paste the contents of this file, and save.
 *   3. Click "Deploy" > "New deployment".
 *   4. Type: Web app.
 *   5. "Execute as": Me.
 *      "Who has access": Anyone.
 *   6. Deploy, then copy the /exec URL, e.g.
 *      https://script.google.com/macros/s/ABCDE.../exec
 *
 * USAGE (from Python):
 *   https://<YOUR_SCRIPT_ID>/exec?url=<urlencoded-zillow-page>&page=2
 *
 * LIMITS: ~6 min per invocation, ~20k UrlFetchApp calls/day. Enough for
 * thousands of pages. If a run needs more, re-invoke.
 */

function doGet(e) {
  var url = e && e.parameter && e.parameter.url;
  var page = (e && e.parameter && e.parameter.page) || "1";
  if (!url) {
    return ContentService.createTextOutput("missing ?url= param")
      .setMimeType(ContentService.MimeType.TEXT);
  }

  // Must target a Zillow origin URL.
  if (!/^https:\/\/www\.zillow\.com\//.test(url)) {
    return ContentService.createTextOutput("only zillow.com allowed")
      .setMimeType(ContentService.MimeType.TEXT);
  }

  var options = {
    muteHttpExceptions: true,
    followRedirects: true,
    headers: {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
    },
  };

  var resp = UrlFetchApp.fetch(url, options);
  var code = resp.getResponseCode();
  var body = resp.getContentText();

  if (code !== 200 || body.indexOf("Access to this page has been denied") !== -1) {
    return ContentService.createTextOutput("ERROR " + code + " page=" + page)
      .setMimeType(ContentService.MimeType.TEXT);
  }

  // Return the raw HTML; the Python client parses __NEXT_DATA__ itself.
  return ContentService.createTextOutput(body).setMimeType(ContentService.MimeType.TEXT);
}
