---
name: recent-buzz
description: Find what's been happening and what people say about a topic recently (30 days by default) on the web, X, Reddit and Xiaohongshu; merge it into a cited report with engagement counts and save it to the library
apps: []
---

Find out what people have been saying about a topic recently and write a report. Only state what the sources say; never invent; quote dates and numbers as given.

1. This needs the internet: dispatch a task from the chat, and put the topic, the time window and the steps below in the goal.
2. Scope:
   - The window is the last 30 days unless the user says otherwise ("this week", "the last three months"). Use that number of days for every `days` below.
   - Channels: always search the web (news, blogs, official announcements). X suits tech, products and public figures;
     Reddit suits first-hand user experience and technical communities; Xiaohongshu suits Chinese consumer, lifestyle and reviews.
     Skip channels that clearly don't fit and say why in the report.
3. Search (call these in the same round so they run in parallel):
   - web_search: one query in the user's language and one in English, with `days`.
   - X: start cheap. For "what's hot lately" topics, read the trending list at https://getdaytrends.com/<country>/ with fetch_url (free),
     then web_search the relevant trends. Use search_posts platform=x only to see the actual discussion: one keyword, with `days`,
     adding lang:en or -is:reply if noisy. X is billed per post and each task has a budget, so usually search it once.
     Read any post links you need in full with fetch_url (free).
   - search_posts platform=reddit: one or two keywords, with `days`.
   - Xiaohongshu: call search_posts platform=xiaohongshu and follow its instructions to search with the computer's browser; read at most 5 to 10 notes.
     If it asks to log in or shows a captcha, call ask_human so the user takes over; don't try it yourself.
   - If a channel answers "can't search", "not filled in" or "search failed", note the reason. Don't force it another way,
     and don't open a search engine in the browser.
4. Read sources: pick the three to six items with the most engagement or information and read them with fetch_url
   (Reddit links come back with comments). X results already contain the post text. Don't read everything.
5. Filter and merge:
   - Drop off-topic items, ads, spam accounts, reposts and anything outside the window.
   - When the same story shows up on several channels, merge it into one item and say so ("discussed on both X and Reddit").
   - Rank by engagement (likes, comments, reposts, score) and freshness.
6. Write the report in Markdown:
   - First line: topic, window (start–end date), one-sentence takeaway.
   - **Key findings**: three to six. Each has a bold one-line finding, two or three sentences, and sources (link, channel, date, engagement).
   - **Dissent**: disagreements, controversy, negative takes. If none, write "No clear disagreement seen".
   - **Posts worth reading**: at most six, each with a sentence or two quoted (not whole paragraphs), link and engagement.
   - **Channel status**: one line per channel: "found N relevant", "no relevant discussion", "error: reason", or
     "not set up (Settings → Search & web pages)". Never report a channel you couldn't search as "nobody talking".
   - If nothing solid turned up, say "No reliable discussion found in this period" and list the channels and queries tried. Don't pad with unrelated results.
7. Save it with save_page, titled "Recent buzz: topic (start date–end date)", format=markdown.
8. Reply with the page link and two or three sentences on the highlights. If the user wants to keep tracking it, create a routine from the chat with
   create_routine (e.g. Monday mornings, goal "/recent-buzz topic, last 7 days"); a background task can't create one, so just mention it in the answer.

Cost guide: X is $0.005 per post (10 per search), Reddit is 1 credit per call, DeepSeek search or page reads are $0.00075 each; a typical run costs a few cents.
