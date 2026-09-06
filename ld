name: Meme Coin Radar scan

on:
  schedule:
    # Hourly, at 20 past. GitHub queues scheduled runs under load, so the
    # actual time drifts a few minutes; that is normal and harmless here.
    - cron: "20 * * * *"
  workflow_dispatch:        # lets you run it by hand from the Actions tab

permissions:
  contents: write           # needed to commit the results back to the repo

concurrency:
  group: radar-scan
  cancel-in-progress: false

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 15

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run scan
        env:
          COINGECKO_API_KEY: ${{ secrets.COINGECKO_API_KEY }}
          SOLSCAN_API_KEY: ${{ secrets.SOLSCAN_API_KEY }}
        run: python scan.py

      - name: Commit results
        run: |
          git config user.name "radar-bot"
          git config user.email "actions@github.com"
          git add data/candidates.json state/holders.json
          if git diff --staged --quiet; then
            echo "nothing changed"
          else
            git commit -m "scan: $(date -u '+%Y-%m-%d %H:%M UTC')"
            git push
          fi
