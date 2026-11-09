---
name: weekly-report
description: Turn this week's work into a weekly report (done, in progress, problems, next week) and save it to the library
apps: []
---

Write a weekly report of the user's work. Only state facts you can find; never invent progress.

1. Range: Monday of this week to today by default. If the user names another period (last week, this month), use that.
2. Gather material, earlier sources first:
   - Anything the user pasted in this message (work log, commits, chat excerpts).
   - Recent work and task results in the conversation (see "recent work" and the chat; use recall for older messages).
   - search_memory for library pages and memories from the period, with words like "report progress done meeting plan"
     plus any project names the user mentioned; read_page for pages you want to cite.
3. Write the report in Markdown with four sections:
   - **Done this week**: one sentence each, saying what was achieved (with numbers when there are any) and linking related pages.
   - **In progress**: how far along, and when it should be done.
   - **Problems and risks**: what is blocked and whose help is needed. Write "None" if there are none.
   - **Next week**: three to five items.
   If the material says nothing for a section, write "(nothing in this week's records; anything to add?)" instead of making it up.
4. Save it with save_page, titled "Weekly report (start date – end date)", format=markdown.
5. Reply with the page link and two or three sentences on the highlights. Ask whether the user wants to add or change
   anything; if so, update the same page_id.
