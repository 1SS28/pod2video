# My podcast clips (pod2video)

This repository turns each new episode of your podcast into short vertical clips. The host who is talking lip-syncs to their real voice using [Magic Hour](https://magichour.ai) Talking Photo. It runs on GitHub Actions: there's nothing to install or host.

**How it works**

1. Every day, GitHub checks your podcast feed. When a new episode appears, it picks the best moments and opens an **issue** with free previews of each one, plus what rendering them would cost.
2. You reply on the issue with the clips you want: `/render 1 4`.
3. A few minutes later, the finished clips are attached to the issue's draft release, along with the exact cost.

## Setup (about 10 minutes, nothing to install)

You need a free [GitHub account](https://github.com/signup). A "repository" is just a folder GitHub keeps for you; this one holds your settings, your hosts' photos, and the finished clips.

1. At the top of this page, click the green **Use this template** button, then **Create a new repository**. Give it any name, choose **Private**, and click **Create repository**. (Private keeps your photos and clips to yourself. Don't use the Fork button: forks of a public repository can't be private.)
2. Give it your keys. In your new repository, click **Settings** (top row of tabs), then in the left sidebar **Secrets and variables → Actions**, then the green **New repository secret**. Add these three, one at a time, with the name typed exactly and the key pasted into the big box:
   - `MAGIC_HOUR_API_KEY`, from [magichour.ai/developer](https://magichour.ai/developer?tab=api-keys)
   - `TRANSCRIPTION_API_KEY`, from AssemblyAI (or Deepgram; set `transcription_provider` to match)
   - `LLM_API_KEY`, from Anthropic (or OpenAI; set `llm_model` to match)
3. Open the file `pod2video.yml` (click it in the file list, then the pencil icon to edit). Put in your podcast's feed address, your hosts' names, and the lines for their photos. Click **Commit changes** to save.
4. Add a photo of each host: open the `hosts` folder, click **Add file → Upload files**, drop the photos in, and commit. Use the same file names you wrote in `pod2video.yml`. No photo for someone? Give them a `description` line instead and a cartoon is drawn.
5. Click the **Actions** tab, choose **pod2video** on the left, then **Run workflow** → **Run workflow**. A few minutes later a new item appears under **Issues** with your previews. From now on this happens by itself every morning for each new episode.

## Day to day

- **New episode:** an issue appears. Watch the previews and check the **Who is who?** table. The camera follows those speaker labels, so if the hosts are the wrong way round, add the mapping shown in the issue to your command.
- **Render:** comment `/render 2 5`. Only people with write access to the repo can trigger a render, and a render that would cost more than `max_credits_per_run` is refused.
- **Re-render or retry:** comment `/render` again. Finished renders are reused, so you never pay twice for the same clip.
- **An older episode:** go to **Actions → pod2video → Run workflow** and set the episode, e.g. `2` for the one before the latest.

## Cost

You pay Magic Hour, your transcription provider and your LLM provider directly with your own keys. With the defaults, clips run 15–30 seconds. A typical 23-second clip with two hosts is about 570 Magic Hour credits: ~$0.90 on the Creator plan billed monthly, ~$0.57 billed annually. Transcription and moment selection add roughly $0.30–$0.50 per episode-hour. GitHub Actions is free for this: private repos get 2,000 minutes a month, and an episode takes roughly 10–15.

## Updating

The workflow uses the pod2video engine by version (`@v1`), so fixes arrive without you merging anything.
