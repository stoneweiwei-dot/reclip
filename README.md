# ReClip

ReClip is a self-hosted web archiver and media downloader.

Paste a public URL and ReClip can now create a **single ZIP** containing the page's readable text, source HTML, discoverable images, direct audio/video files, yt-dlp media, and a machine-readable manifest. The original MP4 / MP3 downloader remains available.

## What ZIP PAGE saves

For each URL:

- `content.md` — readable article/body text
- `content.txt` — plain-text version
- `source.html` — fetched source HTML
- `images/` — discoverable `<img>`, `<picture>`, Open Graph/Twitter images, inline CSS background images, and embedded data-image assets
- `media/` — direct `<video>`, `<audio>`, `<source>`, Open Graph media, direct media links, plus media found by yt-dlp
- `manifest.json` — source URLs, local filenames, byte sizes, MIME types, skipped/failed items, and archive limits

Multiple pasted URLs are packed into one ZIP with one folder per page.

## Existing media modes

- MP4 video download
- MP3 audio extraction
- Quality/resolution picker
- Multiple media URLs
- Automatic URL deduplication
- YouTube, TikTok, Instagram, X/Twitter, Reddit, Facebook, Vimeo, Twitch, SoundCloud and 1000+ yt-dlp-supported sites

## Quick start

```bash
brew install yt-dlp ffmpeg
git clone https://github.com/stoneweiwei-dot/reclip.git
cd reclip
./reclip.sh
```

Open **http://localhost:8899**.

Or with Docker:

```bash
docker build -t reclip .
docker run -p 8899:8899 reclip
```

## Usage

### Archive a whole page

1. Leave **ZIP PAGE** selected.
2. Paste one or more public `http://` or `https://` URLs.
3. Click **Create ZIP**.
4. ReClip extracts text and page assets, then downloads the ZIP automatically. A **Save ZIP** button remains available afterward.

### Download media only

1. Select **MP4** or **MP3**.
2. Paste one or more media URLs.
3. Click **Fetch**.
4. Choose video quality when available.
5. Click **Download**.

## Archive limits

Defaults can be changed with environment variables:

- `RECLIP_ARCHIVE_MAX_MB=1024` — total extracted bytes per ZIP job
- `RECLIP_ASSET_MAX_MB=512` — maximum size of one fetched image/media asset
- `RECLIP_PAGE_MAX_MB=25` — maximum fetched HTML/page response
- `RECLIP_HTTP_TIMEOUT=45` — HTTP read timeout in seconds

ReClip rejects localhost, private, link-local and other non-public network targets before fetching. Redirect targets are revalidated.

## Important extraction limits

ReClip works from the HTML returned to the server and from yt-dlp. Pages whose content exists only after JavaScript execution, requires login/cookies, uses anti-bot challenges, or hides assets behind expiring authenticated URLs may not be fully extractable. Failed/skipped asset URLs are recorded in `manifest.json`.

## Stack

- Python + Flask
- Beautiful Soup 4
- Requests
- yt-dlp + ffmpeg
- Vanilla HTML/CSS/JS

## Disclaimer

Use ReClip only for content you are allowed to save. Respect copyright, access controls, website terms, and applicable law.

## License

[MIT](LICENSE)
