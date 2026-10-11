# My podcast clips (pod2video)

This repository turns each new episode of your podcast into short vertical clips. The host who is talking lip-syncs to their real voice using [Magic Hour](https://magichour.ai) Talking Photo. It runs on GitHub Actions: there's nothing to install or host.

**How it works**

1. Every day, GitHub checks your podcast feed. When a new episode appears, it picks the best moments and opens an **issue** with free previews of each one, plus what rendering them would cost.
2. You reply on the issue with the clips you want: `/render 1 4`.
3. A few minutes later, the finished clips are attached to the issue's draft release, along with the exact cost.

## Setup (about 10 minutes)

1. Click **Use this template → Create a new repository** and make it **Private**. A private repo keeps your hosts' photos and your clips to yourself; a fork of a public repo can't be private.
2. Go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `MAGIC_HOUR_API_KEY`, from [magichour.ai/developer](https://magichour.ai/developer?tab=api-keys)
   - `TRANSCRIPTION_API_KEY`, from AssemblyAI (or Deepgram; set `transcription_provider` to match)
   - `LLM_API_KEY`, from Anthropic (or OpenAI; set `llm_model` to match)
3. Edit `pod2video.yml`: your feed URL, your hosts, and a budget.
4. Add a photo of each host to `hosts/` (with their consent), or give a host a `description` instead to get a cartoon avatar.
5. Go to **Actions → pod2video → Run workflow** to preview your latest episode now instead of waiting for tomorrow.

## Day to day

- **New episode:** an issue appears. Watch the previews and check the **Who is who?** table. The camera follows those speaker labels, so if the hosts are the wrong way round, add the mapping shown in the issue to your command.
- **Render:** comment `/render 2 5`. Only people with write access to the repo can trigger a render, and a render that would cost more than `max_credits_per_run` is refused.
- **Re-render or retry:** comment `/render` again. Finished renders are reused, so you never pay twice for the same clip.
- **An older episode:** go to **Actions → pod2video → Run workflow** and set the episode, e.g. `2` for the one before the latest.

## Cost

You pay Magic Hour, your transcription provider and your LLM provider directly with your own keys. With the defaults, clips run 15–30 seconds. A typical 23-second clip with two hosts is about 570 Magic Hour credits: ~$0.90 on the Creator plan billed monthly, ~$0.57 billed annually. Transcription and moment selection add roughly $0.30–$0.50 per episode-hour. GitHub Actions is free for this: private repos get 2,000 minutes a month, and an episode takes roughly 10–15.

## Updating

The workflow uses the pod2video engine by version (`@v1`), so fixes arrive without you merging anything.
