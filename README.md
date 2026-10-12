# pod2video

Turn an audio-only podcast into short vertical video clips. The host who is talking appears on screen and lip-syncs to their real voice using [Magic Hour](https://magichour.ai) Talking Photo, with word-timed captions and your show's branding on top.

![Before: the episode's audio. After: a captioned 9:16 clip of a cartoon avatar lip-syncing the speaker's real voice.](docs/demo.gif)

*Real output: a 29 s clip from [Open Minds … from Creative Commons](https://creativecommons.org/tag/podcast/) (CC BY 4.0, used with the hosts' permission). The face is a cartoon avatar generated from a text description. 720 Magic Hour credits ($0.86). The full walkthrough, with what went wrong, is in [demo.md](demo.md).*

You give it a podcast feed and a photo of each host (or a one-line description, and it draws a cartoon). It picks the best 15–30 second moments, shows you free previews, and renders only the ones you choose. A finished clip costs about **$0.60–$0.90** in Magic Hour credits. The audio is never changed; only the picture is animated.

There are three ways to use it:

| | Good for | Setup |
| --- | --- | --- |
| **On GitHub, no install** | Podcasters. Every new episode shows up as previews; you reply with the clips you want. | [10 minutes, no terminal](template/README.md) |
| **On your computer** | Trying it on one episode, or scripting it. | [Setting up from nothing](#setting-up-from-nothing) below |
| **As code** | Developers learning the Magic Hour API. | [How it uses the Magic Hour API](#how-it-uses-the-magic-hour-api) |

It was built by someone who works with Magic Hour, as a worked example of the API. All the cost numbers come from real runs, and [demo.md](demo.md) records one from start to finish, mistakes included.

## Setting up from nothing

This is for a Mac and assumes you have never used the terminal. On Linux, skip the Homebrew line and install `pipx` and `ffmpeg` with your package manager. On Windows, use the GitHub route above instead.

**1. Open the terminal.** Press ⌘-Space, type `Terminal`, press Enter. Everything below is typed into that window.

**2. Install the two tools pod2video needs.** Paste each line and press Enter. The first installs Homebrew (a tool that installs other tools) and will ask for your Mac password; the second installs Python and ffmpeg. Both take a few minutes.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

```bash
brew install pipx ffmpeg-full
```

(`ffmpeg-full` and not plain `ffmpeg`: the plain one can't draw the captions.)

**3. Download pod2video and install it.**

```bash
git clone https://github.com/1SS28/pod2video.git && cd pod2video && pipx install . && pipx ensurepath
```

Then close the terminal window and open a new one, so it knows about the `pod2video` command. Type `cd pod2video` to get back into the folder.

**4. Get your three keys.** Each site gives you a long string of letters; keep the tabs open.
- **Magic Hour** does the animation: [magichour.ai/developer](https://magichour.ai/developer?tab=api-keys) → Create new API Key. Your account needs credits; a clip costs about 600–700.
- **AssemblyAI** turns speech into text: [assemblyai.com](https://www.assemblyai.com). Free to sign up, $50 of credit included, which covers hundreds of episodes.
- **Anthropic** picks the best moments: [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key. Add $5 of credit; an episode uses about 10 cents.

**5. Put the keys in the settings file.**

```bash
cp .env.example .env && open -e .env
```

A text editor opens. Paste each key after the matching `=` sign (`MAGIC_HOUR_API_KEY=`, `TRANSCRIPTION_API_KEY=`, `LLM_API_KEY=`), no spaces, then save and close it.

**6. Check it worked.**

```bash
pod2video doctor
```

You want four ticks. A ✗ tells you which key is missing or wrong.

**7. Make clips.** Swap in your feed address and your hosts' photos (drag a photo into the terminal window to paste its path):

```bash
pod2video run --feed https://example.com/feed.xml --host "Alice=alice.jpg" --host "Bob=bob.jpg" --preview
```

This costs no Magic Hour credits. It asks which voice belongs to which host (read the sample lines; don't guess), then writes six preview videos to the `out` folder. Watch them, then render the ones you want:

```bash
pod2video run --feed https://example.com/feed.xml --host "Alice=alice.jpg" --host "Bob=bob.jpg" --pick 1,4
```

It shows the cost and asks before spending. Two minutes later the finished clips are in the same folder. No photo? Write `--host "Bob=desc:tall, short beard, green beanie"` and it draws a cartoon of Bob instead (about 50 credits, once).

[demo.md](demo.md) walks through a real run of exactly these steps.

## On GitHub, with no install

Make a private repository from the [template](template/), add your three keys as secrets, and GitHub checks your feed every day. Each new episode gets an issue with free previews and the cost; you reply `/render 1 4` and the clips are attached. There's no server and nothing on your computer. The [template README](template/README.md) says exactly where to click.

```
new episode in the feed ──► transcribe, pick moments, free previews
                            └─► an issue: the candidates, preview links, cost, "who is who?"
you comment "/render 1 4" ──► those clips rendered with Magic Hour (budget-capped)
                            └─► a reply with download links and the exact bill
```

## How it works

1. **Fetch:** download the episode (cached), or use `--audio`.
2. **Transcribe:** word timestamps and speaker labels from AssemblyAI or Deepgram, cached by audio hash.
3. **Map speakers:** shows each speaker's talk time and a sample line, and asks which one is Alice and which is Bob. The answer is saved per feed. Skip the prompt with `--speaker-map "A=Alice,B=Bob"`.
4. **Select moments:** Claude (or GPT) reads the transcript as numbered sentences and ranks the best self-contained moments: a hook, a complete thought, something quotable. Cuts land on sentence boundaries. The result is saved as `moments.json`: edit it and rerun with `--moments`.
5. **Plan shots:** the camera cuts to whoever is talking, holds through short interjections, and never cuts faster than every 1.5 s.
6. **Animate:** only the host on screen is animated. Each host's on-screen moments in a clip are joined into one audio track (with short pauses between them), so a clip needs one Talking Photo job per host. A clip bills about its own length (~3% over) however many hosts there are. Every image passes Magic Hour's free face detection first, and finished renders are reused on reruns.
7. **Compose:** ffmpeg cuts each render back into its shots and builds the 1080×1920 frame:
   - the original audio;
   - the speaker on a rounded 960×960 card with a drop shadow, over a blurred, darkened copy of their own image (Talking Photo outputs up to 720p, so a square card stays sharp where full height would look soft);
   - a slow push-in on every shot, with alternate shots of each host punched in tighter like a second camera (a single host gets cuts at natural pauses);
   - big captions underneath that pop in phrase by phrase and highlight each word as it's spoken;
   - the clip title as a hook for the first 3 s;
   - a faint audio waveform and a progress bar;
   - the podcast name, episode title and optional logo.
8. **Report:** `out/<episode>/report.json` and a summary table with credits, dollars on your plan and on the other plans, time per stage, and failures.

### Layouts

| `--layout` | What you get | Billed per 23 s clip, 2 hosts |
| --- | --- | --- |
| `speaker-cut` (default for 2+ hosts) | Whoever is talking, full width; cuts between hosts | ~24 s |
| `split` | All hosts stacked, each animated for the whole clip | 46 s |
| `single` (default for 1 host) | One host | 23 s |

### Avatars

- `--host "Alice=alice.jpg"`: animate the photo as given (default).
- `--avatar cartoon`: turn each photo into a cartoon once (~50 credits per host, cached and reused for every episode).
- `--host "Bob=desc:tall, beard, green beanie"`: no photo needed; a cartoon is generated from the description.

Run `pod2video avatars --host ...` to make avatars up front so that `--preview` shows them.

## Cost

Talking Photo is billed per second of output: **about 24 credits/s in `prompted` mode** (the default, up to 45 s per job; the docs say 20, a real job billed 1 credit per frame at 24 fps) or **48 credits/s in `realistic` mode** (`--generation-mode realistic`, best likeness). These rates are from Magic Hour's [model pricing](https://docs.magichour.ai/api-reference/models). Clips default to 15–30 s, because short clips hold attention better and cost proportionally less. Per typical 23 s two-host clip:

| Setup | Credits | Creator monthly ($19) | Creator annual ($12/mo) |
| --- | --- | --- | --- |
| speaker-cut, prompted (default) | ~570 | $0.90 | $0.57 |
| speaker-cut, realistic | ~1,140 | $1.80 | $1.14 |
| split, realistic (every host, whole clip) | 2,210 | $3.50 | $2.21 |

**Which plan?** `pod2video plans` compares every option for your volume. A weekly show with 3 clips per episode (~7,400 credits a month) fits **Creator**: $19/mo, or $12/mo billed annually. Pro and Business add credits and concurrency, but not Talking Photo quality, which is capped at 720p on every paid plan. Credit packs work without a plan but render at the lowest resolution. Subscription credits never expire. pod2video reads your plan from your API key (`MAGIC_HOUR_PLAN=auto`) to price every report. Override it with `--plan`.

Other costs per episode-hour: transcription about $0.23 (AssemblyAI) or $0.26 (Deepgram), and moment selection about $0.10–$0.30 with Claude Opus.

**Real run (2026-10-11):** one 30 s clip from a 25-minute episode, cartoon avatar, `prompted` mode, speaker-cut.

| | |
| --- | --- |
| Magic Hour, Talking Photo (1 job) | 720 credits = $0.86 at the usage-based Creator rate ($0.72 on Creator annual) |
| Magic Hour, 2 cartoon avatars (one-time) | 100 credits = $0.12 |
| Moment selection, Claude Opus 5.5, run three times | $0.24 |
| Transcription, AssemblyAI, 25 min | ~$0.10 |
| Time | transcribe ~1 min, select moments 20 s, Talking Photo 84 s, compose 6 s |

Details and the terminal output are in [demo.md](demo.md).

Every Magic Hour job's `credits_charged` is logged to `out/<episode>/ledger.jsonl` as it happens, and the final amount is read back once the job finishes. Failed jobs are refunded by Magic Hour, and the ledger records them anyway. `--max-resolution` lowers output size but not credits.

## How it uses the Magic Hour API

All the Magic Hour calls are in [core/magichour.py](core/magichour.py) (plain REST with `httpx`) and [core/plans.py](core/plans.py), written to be read and reused.

| Step | Endpoint | What to know |
| --- | --- | --- |
| Check the account | `GET /v1/account` | Returns the tier, credit balance and subscription price, so you can work out what a credit costs this user and refuse a run they can't afford. |
| Upload inputs | `POST /v1/files/upload-urls`, then `PUT` to the returned URL | Ask for one URL per file (`type` + `extension`), `PUT` the bytes, and pass the returned `file_path` to jobs. Don't send your API key to the presigned URL. Upload each file once and reuse its `file_path`. |
| Check faces | `POST /v1/face-detection`, poll `GET /v1/face-detection/{id}` | Free at the time of writing. A cheap preflight that stops a run before any credits go on an image with no detectable face. |
| Make avatars | `POST /v1/ai-image-editor` (photo → cartoon) or `POST /v1/ai-image-generator` (description → cartoon), poll `GET /v1/image-projects/{id}` | A flat cost per image (50 credits for `nano-banana` at 1k). Cache the result: a host's avatar never changes. |
| Animate | `POST /v1/ai-talking-photo` | `start_seconds` and `end_seconds` are required and select part of the audio. Billed per second of output: about 24 credits/s in `prompted` mode (1 credit per 24 fps frame; the docs say 20) (up to 45 s per job) or 48 credits/s in `realistic` mode (up to 300 s). `max_resolution` caps the output (720p at most) but doesn't change credits. |
| Wait and download | `GET /v1/video-projects/{id}` | Poll until `complete`, `error` or `canceled`, then download `downloads[0].url`. The `credits_charged` returned on create is an estimate; read the final value here. Failed jobs are refunded, and this field is updated to show it. |

**Patterns worth copying:**

- **Spend only on what's on screen.** Talking Photo bills per second of output, so pod2video animates only the host who is visible and joins each host's on-screen moments into one job per clip, with short silent gaps that are trimmed back out. Two hosts cost about the same as one: roughly 24 billed seconds per 23 s clip instead of 46.
- **Free stand-ins before paid calls.** Previews use the real layout with still images, so people choose clips before any credits are spent.
- **Cache by input hash.** Renders are keyed by the SHA-256 of their audio and image plus the settings, so a retry never pays twice.
- **Log every credit.** Each job's estimated and final `credits_charged` is written to a JSONL ledger as it happens. That's where the cost numbers in this README come from.

## Options

```
--feed URL | --audio FILE          source (one of)
--episode latest|N|text            N = 1 is the latest (see `pod2video episodes --feed URL`)
--host "Name=photo.jpg" | "Name=desc:..."   repeat per host
--clips 3  --candidates 6  --pick 1,4,5  --min-len 15  --max-len 30
--layout speaker-cut|split|single  --style bold|clean|minimal
--preview  --dry-run  --yes
--avatar photo|cartoon  --avatar-prompt TEXT
--generation-mode prompted|realistic  --plan creator-annual  --per-month 4
--segment 95-130                   fixed time range(s) instead of LLM selection
--moments FILE                     use an edited moments.json
--podcast-name / --episode-title / --logo PATH|feed
```

## Try it with no API keys (macOS)

Makes a fake 90-second two-voice episode with the Mac's built-in voices, then previews it, so you can see the layout without any accounts.

```bash
python3 examples/synthetic_demo.py
pod2video run --audio examples/demo/demo.mp3 --podcast-name "Side Project Radio" \
  --host "Sam=examples/demo/sam.png" --host "Dan=examples/demo/dan.png" \
  --speaker-map "A=Sam,B=Dan" --moments examples/demo/moments.json --preview
```

## Consent

Only animate photos of people who have agreed to it. Cartoon avatars are a good option for anyone who would rather not have their real face animated. pod2video animates images only: voices and words are never changed, and every clip uses the original audio. Make it clear to viewers that the video is an animated image.

## Project layout

```
core/            reusable across projects
  magichour.py   uploads, talking_photo(), image edit/generate, face detection, voice_generate(), polling, credit logging
  plans.py       Magic Hour plans, price per credit, plan detection, cheapest plan for a volume
  llm.py         provider-agnostic JSON-schema calls (Anthropic / OpenAI)
  compose.py     ffmpeg: 9:16 layouts, shot placement, ASS captions, overlays
  costs.py       cost ledger + totals
pod2video/       cli, feed, transcribe, speakers, moments, audio, avatars, pipeline
                 showconfig (pod2video.yml), github + actions (the GitHub Actions flow)
action.yml       the composite GitHub Action: `uses: 1SS28/pod2video@v1`
template/        the repository each show copies (workflow, pod2video.yml, hosts/)
examples/        synthetic demo, demo-GIF maker, moments.json format
tests/           pytest; Magic Hour, GitHub, transcription and LLM APIs are mocked
```

## Publishing (maintainers)

The engine lives at `1SS28/pod2video` and the template at `1SS28/pod2video-template`.

1. Push this folder to `1SS28/pod2video`, then tag the release: `git tag v1.0.0 && git tag v1 && git push origin main --tags`. For each compatible release after that, move `v1` forward: `git tag -f v1 && git push -f origin v1`.
2. Publish the contents of `template/` as `1SS28/pod2video-template` (a snapshot of the folder as its own repository), and turn on **Settings → Template repository**.

## License

MIT
