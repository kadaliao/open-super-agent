---
name: save-webpage
description: Save a web page to the library as clean Markdown, without menus or ads, keeping the source link so you can search it later
apps: []
---

Save the web pages the user gives you to the library. There may be several links; save each one as its own page.

1. This needs the internet: from the chat, dispatch one task whose goal lists every link and the steps below.
2. Get the text: try fetch_url first. If that fails or the text is clearly incomplete (needs a login, a "read more" click,
   or only says "please enable JavaScript"), open the address in the computer's browser and read it there
   (browser_open, browser_read). If the site needs the user to log in, ask the user to take over the computer.
3. Turn it into Markdown:
   - Keep only the article: drop navigation, menus, ads, related reading, comments and "share to" buttons.
   - Keep the original structure: headings, lists, tables, code blocks, quotes; write images as `![caption](image URL)`.
   - Do not rewrite, summarize or translate the original. If it is very long, keep the first part whole and end with
     "(continues in the original)".
   - Start with a quote block: the source link, the author and publish date if shown, and today's date as the date saved.
4. Save it with save_page, using the page's own title, format=markdown.
5. In the final answer, list each saved page's title and link; for any page not saved, say why (would not open, paywall,
   needed a login the user did not do).
