# cf-download-bot

Send a page link -> bot opens it in Chrome (Botasaurus), logs in to the site (once, session is saved in profiles/), clicks Download, passes the
Cloudflare Turnstile, downloads the file and uploads it to Telegram (wzgram + tgcrypto).

## Setup
```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt update
sudo apt install -y ./google-chrome-stable_current_amd64.deb
google-chrome --version

sudo apt-get install -y xvfb ffmpeg      # plus Google Chrome
pip install -r requirements.txt
nano .env                                # fill API_ID, API_HASH, BOT_TOKEN
                                 ./run.sh# or: xvfb-run -a python3 bot.py
```
(If your file explorer hides dotfiles, `ENV_FILL_ME.txt` is the same content: copy it to `.env`.)

## Files
- bot.py        Telegram side: queue, progress, upload
- scraper.py    Browser side: open page, click Download, Turnstile, get link
- downloader.py Fetch the file with the browser's cookies / UA
- config.py     Reads .env

## Notes
- Keep HEADLESS=false (headless gets flagged by Cloudflare).
- Datacenter IPs (Codespaces/EC2) may fail Turnstile -> set PROXY (residential) or run on your own PC.
- Debug screenshots are saved in output/screenshots/.
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------DEVELOP BY RAJAT NAYEK------------------------------------
