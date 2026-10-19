# The README demo. Synthetic persona, offline fixtures, real commands. From the repo root:
#
#     uv sync && vhs docs/demo/demo.tape
#
# Needs VHS (https://github.com/charmbracelet/vhs; `brew install vhs`). Writes docs/demo.gif.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1200
Set Height 780
Set Padding 18
Set WindowBar Colorful
Set TypingSpeed 30ms
Set Theme "Dracula"
Set WaitPattern /^\$ ?$/
Set WaitTimeout 60s

Hide
Type "source docs/demo/setup.sh && clear" Enter
Wait
Show

Type "# Synthetic persona 'intl-sponsorship', offline fixtures. Check the machine first:" Enter
Type 'jobhunt doctor --fix --offline --human | grep -vE "migration|create /"' Enter
Wait
Sleep 3.5s

Hide
Type "persona && clear" Enter
Wait
Show

Type "# Onboarding is done. Fetch, filter, rank and read the application forms:" Enter
Type "jobhunt run --human" Enter
Wait
Sleep 7s

Hide
Type "clear" Enter
Show

Hide
Type "judge && clear" Enter
Wait
Show

Type "# Your AI tool did the judgment tasks (jobhunt-judge); a scripted stand-in here." Enter
Type "jobhunt ingest > /dev/null && jobhunt match --human | head -n 4" Enter
Wait
Sleep 3s
Type "jobhunt digest --human | head -n 22" Enter
Wait
Sleep 7s

Hide
Type "clear" Enter
Show

Type "# You applied and heard back. Record it here or in the sheet's Status column:" Enter
Type "jobhunt mark 10 Applied >/dev/null && jobhunt mark 10 Interviewing >/dev/null" Enter
Wait
Type "jobhunt mark 1 Applied >/dev/null && jobhunt mark 1 Rejected >/dev/null" Enter
Wait
Type "jobhunt mark 5 Applied >/dev/null && jobhunt mark 3 Skipped --reason domain >/dev/null" Enter
Wait
Sleep 1s
Type "jobhunt calibrate --human" Enter
Wait
Sleep 8s
