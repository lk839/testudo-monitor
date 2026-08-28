# Testudo Monitor — GitHub Actions Version

Use this version if your Mac will be off and you do not want a credit card/cloud VM.

## Important recommendation

Make the repository PUBLIC if you are comfortable with the code being visible.
Do NOT put your ntfy topic in any file. Store it only as a GitHub Actions secret.

## Setup

1. Create a GitHub account.
2. Create a new repository named `testudo-monitor`.
3. Choose Public.
4. Upload ALL files from this folder, including the hidden `.github` folder.
5. In the repository open:
   Settings -> Secrets and variables -> Actions -> New repository secret
6. Name:
   NTFY_TOPIC
7. Value:
   your private ntfy topic name
8. Open the Actions tab and enable workflows if GitHub asks.
9. Open "Testudo Seat Monitor" and click "Run workflow" once.
10. Check the run log. The first run establishes baseline only.

## iPhone

Install ntfy and subscribe to the same topic.

## Monitoring schedule

GitHub wakes the workflow every 5 minutes, but the program itself only contacts Testudo when due:

- Aug 28-30: 10 min daytime / 30 min overnight
- Aug 31-Sep 4: 5 min daytime / 15 min overnight
- Sep 5-13: 10 min daytime / 30 min overnight
- Sep 14: 5 min daytime / 15 min overnight
- After Sep 14: no Testudo checks

Daytime = 7 AM to 11 PM Eastern.

The program contacts only one sentinel page first. It scans other watched courses only if the Testudo snapshot changes.

## Safety

- Full-scan pages are requested sequentially.
- 3-5 seconds between full-scan pages.
- 403/429 => two-hour pause.
- Server/network problem => 30-minute pause.
- No parallel Testudo requests.
- No tight retry loop.
