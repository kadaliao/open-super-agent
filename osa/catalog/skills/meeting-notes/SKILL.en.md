---
name: meeting-notes
description: Turn a meeting transcript, recording text or rough notes into minutes (decisions, action items with owner and due date, open questions) and save them to the library
apps: []
---

Turn the user's meeting material into meeting minutes. The material may be text pasted in the message or an attachment
("[附件] path").

1. Get the original: use pasted text directly. For an attachment, dispatch a task whose goal includes the full path, and have the
   Worker read it with computer_read_file and write the minutes. If there is too little to tell what the meeting was about,
   ask the user first instead of guessing.
2. Write it in Markdown, in this order:
   - A first line with the topic, date and attendees (only what the material says).
   - **Decisions**: what was settled, one sentence each.
   - **Action items**: a table with "Item / Owner / Due". Write "TBD" when the material names no owner or date; never assign one yourself.
   - **Open questions**: what still needs discussion or someone's reply. Write "None" if there are none.
   - **Discussion**: a short paragraph per topic with each side's view; no need to quote line by line.
   Only write what is in the material; drop filler, repetition and digressions. Copy names, numbers and dates exactly,
   and mark anything uncertain "(to confirm)".
3. Save it with save_page, titled "Meeting notes: topic (date)", format=markdown.
4. Reply with the page link and list the action items. For items with a due date, ask whether the user wants reminders
   (only after they agree, create one-time reminders with create_routine).
