# Tool truth (Phase 0C)

Verified 2026-10-23 from primary sources (each row cites one). Live checks run on this machine: wigolo search/fetch/diff from the Python stdio client (`bin/gtm tools check`), last30days v3.30.0 (Reddit + HN, keyless), mem0 2.2.1 + Qdrant 1.20.0 round-trip with two isolated products. Apify and Snov live checks need the founder's keys (`bin/gtm tools check --paid`).

## Design decisions this file drives
- **Sending:** one Snov list + one campaign per (product, mailbox). `add-prospect-to-list` auto-starts the drip, so a prospect is only added after signature + every gate passes. The whole rendered sequence travels as custom fields (`gtm_subject`, `gtm_body_1..3`); the campaign steps are just `{{gtm_body_N}}` (custom fields: up to 30, 65,000 chars each; created in the Snov UI).
- **Suppression:** global Snov do-not-email list (`/v2/blacklists/{id}/items`) + `change-recipient-status Unsubscribed` in every campaign we own.
- **Kill switch:** `POST /v2/campaigns/{id}/action {"action":"pause"}` for every campaign of the paused mailbox/product.
- **Replies:** `/v2/campaigns/{id}/all-replies` (reply text) + `recipients-activity` (bounce/unsubscribe events).
- **Reply drafts:** Snov has no send/reply endpoint, so approved drafts are sent by the founder from the mailbox (`sent_manual`).
- **Catch-all:** `smtp_status=unknown` + `unknown_status_reason=catchall` -> `email_class=catchall`.
- **Erasure:** no delete-prospect endpoint: erasure adds the address to the DNE list and marks it Unsubscribed in every campaign; the report lists the manual Snov delete.
- **Rate limit:** 60 req/min for the whole Snov API: the client paces itself at 1 req/s.

---

## 1. Snov.io REST API

### 1.0 General

| Fact | Value | Source |
|---|---|---|
| Base URL | `https://api.snov.io` (v1 and v2 paths) | [S] |
| Rate limit | **60 requests per minute** (whole API, one global number) | [S] intro |
| What the API returns on rate-limit (429 body / Retry-After) | **UNCONFIRMED**, not documented | [S] |
| Free plan | No API access by default; test access only via a booked demo | [S] intro |
| Auth header | `Authorization: Bearer <access_token>` | [S] Authentication |
| v1 code samples | pass `access_token` as a form/query param instead of the header (e.g. get-user-campaigns, add-prospect-to-list, get-user-lists, lists, get-balance, get-domain-emails-count). Whether a Bearer header alone works on v1 is **UNCONFIRMED** | [S] code samples |
| Async pattern (v2) | `POST .../start` returns a `task_hash`; then `GET .../result...` until `status` = `completed` (`in_progress` while running). Most start calls also accept `webhook_url` so results are pushed instead of polled | [S] |

### 1.1 Auth

| Item | Value | Source |
|---|---|---|
| Endpoint | `POST https://api.snov.io/v1/oauth/access_token` | [S] Authentication |
| Body (form) | `grant_type=client_credentials`, `client_id`, `client_secret` (from https://app.snov.io/account/api) | [S] |
| Response | `{"access_token": "...", "token_type": "Bearer", "expires_in": 3600}` | [S] |
| Lifetime | `expires_in` is in seconds: **3600 (1 h)**. No refresh token; request a new one | [S] |

### 1.2 Email verification (v2 async)

| Item | Value | Source |
|---|---|---|
| Start | `POST https://api.snov.io/v2/email-verification/start` | [S] Email Verifier |
| Params | `emails[]` (repeat once per email), optional `webhook_url`. **Max 10 emails per request.** Python sample sends them as `params=` (query string) on the POST | [S] |
| Start response | `{"data":{"task_hash":"..."},"meta":{"emails":[...]}}` | [S] |
| Result | `GET https://api.snov.io/v2/email-verification/result?task_hash=<hash>` (`task_hash` is a **query param**, not a path segment) | [S] |
| Result shape | `{"status":"completed"\|"in_progress","data":[{"email":"...","result":{"smtp_status":..., "is_valid_format":bool, "is_disposable":bool, "is_webmail":bool, "is_gibberish":bool, "unknown_status_reason":"..."}}],"meta":{"emails":[...],"task_hash":"..."}}` | [S] |
| `smtp_status` values | `valid`, `not_valid`, `unknown` (shown in the UI as "Unverifiable") | [S] |
| Catch-all | **No separate status.** A catch-all is `smtp_status: "unknown"` + `unknown_status_reason: "catchall"` | [S] example + field table |
| `unknown_status_reason` values | `banned` (could not verify with certainty), `catchall`, `connection_error`, `greylist`, `hidden_by_owner` (owner requested removal). Docs capitalise them (`Banned`, `Catchall`, `Connection_error`, `Greylist`, `Hidden_by_owner`); response examples are lowercase (`catchall`, `banned`). Match case-insensitively | [S] |
| Field only present when unknown | `unknown_status_reason` is omitted for `valid`/`not_valid` in the examples | [S] example |
| Cost | 1 credit for a `valid` or `not_valid` result; **`unknown` is not charged**. "The API follows the same credit rules as the web app" | https://snov.io/knowledgebase/email-verification-credits/ |

### 1.3 Email finder: name + domain (v2 async)

| Item | Value | Source |
|---|---|---|
| Start | `POST https://api.snov.io/v2/emails-by-domain-by-name/start`. Docs recommend a **raw JSON body**: `{"rows":[{"first_name","last_name","domain"}], "webhook_url"?}`. **Max 10 rows per request** | [S] Find emails from name and domain |
| Start response | `{"data":{"task_hash":"..."},"meta":{"rows":[...]}}` | [S] |
| Result | `GET https://api.snov.io/v2/emails-by-domain-by-name/result?task_hash=<hash>` | [S] |
| Result shape | `{"status":..., "data":[{"people":"John Doe","result":[{"email","smtp_status","is_valid_format","is_disposable","is_webmail","is_gibberish","unknown_status_reason"?}]}], "meta":{"rows":[...],"task_hash"}}` | [S] |
| `status` values | `completed`, `in_progress`, **`not_enough_credits`** | [S] |
| `smtp_status` here | only `valid` or `unknown` (same reasons as 1.2) | [S] |
| Gotcha | Results are keyed by `people` (the full-name string), **not** by the input row's index or domain. Map them back yourself | [S] example |
| Cost | 1 credit for each email with a `valid` or `unknown` status | [S] |

### 1.4 Domain search / emails by domain

All v2 and async. **No endpoint returns an email *pattern*/format string.** You have to infer the pattern from the returned emails plus names. Source: [S]; "pattern" does not appear anywhere in the API reference.

| Step | Method + path | Inputs | Output (key fields) | Cost | Source |
|---|---|---|---|---|---|
| Company info start | `POST /v2/domain-search/start` | `domain`, `webhook_url`? | `meta.task_hash`, `links.result` | 1 credit/unique request; no charge if no results | [S] |
| Company info result | `GET /v2/domain-search/result/{task_hash}` (**path** param) | | `data{company_name, city, founded, website, hq_phone, industry, size, related_domains[]}`, `meta{domain, task_hash, prospects_count, emails_count, generic_contacts_count}`, `links{prospects, domain_emails, generic_contacts}`, `status` = `completed` / `in progress` (docs wording, with a space) | | [S] |
| Domain emails start | `POST /v2/domain-search/domain-emails/start` | `domain`, `webhook_url`?, `next`? (page cursor) | `meta.task_hash`, `links.result` | 1 credit/unique request | [S] |
| Domain emails result | `GET /v2/domain-search/domain-emails/result/{task_hash}` | | `data:[{"email"}]` (**unverified**, email only, no names), `meta{domain, tab:"emails", task_hash, next, total_count}`, `links.next` (full URL to the next start call), `status`. **Up to 50 emails/page** | | [S] |
| Generic contacts | `POST /v2/domain-search/generic-contacts/start` then `GET .../generic-contacts/result/{task_hash}` | `domain` | `data:[{"email"}]`, `meta.tab:"service"`, `total_count` | 1 credit/unique request | [S] |
| Prospects (names) start | `POST /v2/domain-search/prospects/start` | `domain`, `positions[]` (max 10), `page` (20 per page), `webhook_url`? | task_hash | 1 credit/unique request | [S] |
| Prospects result | `GET /v2/domain-search/prospects/result/{task_hash}` | | `data:[{first_name,last_name,position,source_page,search_emails_start}]`, `meta.total_count`, `links.next` | | [S] |
| Prospect email | `POST /v2/domain-search/prospects/search-emails/start/{prospect_hash}` then `GET .../search-emails/result/{task_hash}` | | `data{searching_date, emails:[{email, smtp_status(valid\|unknown)}]}` | 1 credit per prospect with an email | [S] |
| Count only (free) | `POST /v1/get-domain-emails-count` | `domain` (+ `access_token` in samples) | `{"success":true,"domain","webmail":bool,"result":<int>}` (always 0 for webmail) | Free | [S] |

Gotcha: the name+domain finder uses `?task_hash=` as a query param, but the domain-search results use `/{task_hash}` in the path.

### 1.5 Prospects and lists

**Add prospect to list:** `POST https://api.snov.io/v1/add-prospect-to-list` (Free). [S]

| Field | Notes | Source |
|---|---|---|
| `listId` | **required**. Integer list id | [S] |
| `email` | the prospect's email. Required unless `socialLinks[...]` is given | [S] |
| `fullName`, `firstName`, `lastName` | | [S] |
| `phones` | array | [S] |
| `country` | must be a name from Snov's country list | [S] |
| `locality`, `position`, `companyName` | | [S] |
| `companySite` | use the `http://example.com` format | [S] |
| `socialLinks[linkedIn]` | **required if email is null**. Networks: LinkedIn, Facebook, X | [S] |
| `customFields[<name>]` | value for a custom field **you already created**. The name goes in the brackets: `customFields[specialization]=...` (form), or `"customFields":{"specialization":"..."}` in the JSON example | [S] |
| `updateContact` | `true`/`false`: when true and a prospect with this email exists in one of the lists, the existing profile is updated | [S] |
| `createDuplicates` | `true`/`false`. **Only one of `updateContact` / `createDuplicates` can be true** | [S] |
| Response | `{"success":true,"id":"0Y2Q...==","added":true,"updated":false}` plus `errors` on failure | [S] |
| Side effect | Adding to a list that has an active campaign **auto-starts the drip for that prospect** | [S] method description |

Custom field limits (from the knowledge base, not the API page):
- **Max 30 custom fields.** The KB says both "30 custom fields per list" and "You can create up to 30 custom fields". Custom fields are account-wide. https://snov.io/knowledgebase/how-to-manage-custom-fields-and-data-tabs/ , https://snov.io/knowledgebase/prospect-data-in-snov-io/
- Value: up to **65,000 characters**. Field title: up to **255 characters**. Use `<br>` for line breaks. https://snov.io/knowledgebase/how-to-manage-custom-fields-and-data-tabs/
- Types: string, number, date. `GET /v1/prospect-custom-fields` returns `[{key:"customFields['company']", label, required, type}]`. [S]
- Custom fields cannot be created through the API: there is no create-custom-field endpoint in [S]. Treat this as **UNCONFIRMED**; it is only an absence.

Other list and prospect endpoints:

| Purpose | Method + path | Key params / response | Source |
|---|---|---|---|
| Get user lists | `GET /v1/get-user-lists` | `[{id, name, contacts, isDeleted, creationDate{date,timezone_type,timezone}, deletionDate}]`. **Includes deleted lists**: filter on `isDeleted` | [S] |
| Create list | `POST /v1/lists` | `name`. Response `[{"success":true,"data":{"id":1234567}}]` (**wrapped in an array**) | [S] |
| View prospects in list | `POST /v1/prospect-list` | `listId` (req), `page`, `perPage` (max 5000). Response `{success, list{name,contacts,creationDate,emailsCount}, prospects:[{id,name,firstName,lastName,emails:[{email,probability,isVerified,jobStatus,domainType,isValidFormat,isDisposable,isWebmail,isGibberish,smtpStatus}]}]}`. Prospects come in reverse order (newest first). `smtpStatus` is numeric/null here (e.g. `3`) and its mapping is **UNCONFIRMED** | [S] |
| Prospect by id | `POST /v1/get-prospect-by-id` | `id`. Includes the lists and `campaigns` the prospect is in (status, sent, opens, replies) | [S] |
| Prospect by email | `POST /v1/get-prospects-by-email` | | [S] |
| Delete a prospect | **Not available**: no such endpoint in [S] | [S] (absence) |

### 1.6 Campaigns, recipients, analytics, DNE, webhooks

**Campaign management**

| Purpose | Method + path | Notes | Source |
|---|---|---|---|
| List campaigns | `GET /v1/get-user-campaigns` (Free) | `[{id, campaign (=name), list_id, status, created_at, updated_at, started_at (unix ts), hash}]`. `status` examples: `Paused`, `Draft` | [S] |
| Create campaign | `POST /v2/campaigns/create` | JSON. Created with status `new`, **without message content**: add it afterwards via `POST /v2/campaigns/{campaign_id}/steps/{step_id}/content/create` | [S] |
| Get / update | `GET` / `PATCH /v2/campaigns/{campaign_id}` | | [S] |
| **Pause / resume a whole campaign** | `POST /v2/campaigns/{campaign_id}/action`, JSON `{"action": "start"\|"pause"\|"resume"\|"complete"\|"archived"}` (Free). Response `{"success":true}` / `errors` | **This is available via the API** | [S] Change campaign state |
| Delete campaign | `DELETE /v2/campaigns/{campaign_id}` | only in status `new`/`complete`/`archived`; active/pause/scheduled must be stopped first | [S] |
| Schedules | `GET /v2/campaigns/schedules` | | [S] |

**Campaign settings exposed on create (so they are API-settable, not UI-only)**. Source: [S] Create campaign.
- `tracking.open` (bool), `tracking.link_click` (bool). `tracking` is required for email campaigns.
- `sending_settings.skip_who_replied` (stop follow-ups for prospects who reply).
- `sending_settings.skip_company_domain_who_replied` (bool): **"stop on company reply"**, i.e. skip recipients from a company domain where anyone replied.
- `sending_settings.one_click_unsubscribe`, `skip_unverifiable`, `skip_unverified`, `skip_recipients_added_to_my_another_campaign`, `skip_recipients_added_to_team_another_campaign`, `daily_sending_all`, `daily_sending_new_recipients`, `sending_priority` (`first_email`\|`follow_up`).
- `recipients.list_id` (required), `recipients.black_list_id` (DNE list id), `email_accounts[]`, `priority` (`low`\|`medium`\|`high`), `complete_campaign_after_last_step` (required), `archive_in_months` (required: 1, 2, 3 or 6), `provider_matching` (required for email), `title` (max 255).
- Whether `PATCH /v2/campaigns/{id}` can change tracking/skip settings on a running campaign is **UNCONFIRMED** (not read in detail).

**Recipients**

| Purpose | Method + path | Notes | Source |
|---|---|---|---|
| Check recipient status | `GET /v2/campaigns/{campaign_id}/recipient?emails[]=...` (1–100 emails, unique, max 100 chars each) | `data:[{email, exist:bool, status}]`. Status examples: `tocheck`, `active`, `finished`, `unsubscribe`, `moved`; `null` if not present. **Lowercase here** | [S] |
| Change recipient status | `POST /v1/change-recipient-status` (Free) | `email` (req), `campaign_id` (req), `status` (req) = **`Active` \| `Paused` \| `Unsubscribed`** (capitalised). Cannot change recipients that are `Finished` or `Moved`. Response `{"success":true}` / `success:false` + error | [S] |
| Scope | **Per campaign only** (`campaign_id` is required). There is no "stop in all campaigns" call. Use the do-not-email list for global suppression (campaigns check recipients against `recipients.black_list_id`) | [S] |
| Finished prospects | `GET /v1/prospect-finished?campaignId=` | `[{id, prospectId, userName, userEmail, campaign, hash}]` | [S] |

**Do-not-email (DNE) lists** (all Free). Source: [S].

| Purpose | Method + path | Notes |
|---|---|---|
| Create | `POST /v2/blacklists` | `name` (unique, ≤100 chars, max 200 lists). 422 if the name is taken or the limit is reached. Response `data{list_id,name}` |
| **Add items (current)** | `POST /v2/blacklists/{list_id}/items` | `items[]`: emails, domains or `linkedin.com/in/...` URLs, **≤100 per request**. Response `data{added[], duplicates[], rejected[{item,reason}]}`. Reasons: `Invalid email address` \| `Invalid domain` \| `Invalid LinkedIn profile URL`. 404 if the list is missing |
| Add (deprecated) | `POST /v1/do-not-email-list` | `items`, `listId` (req). Response `[{"success":true,"data":{"duplicates":[]}}]` |
| Delete items | `DELETE /v2/blacklists/{list_id}/items` | same `items[]`. Response `deleted[]/not_found[]/rejected[]`. Recipients already blocked stay blocked |
| List lists | `GET /v2/blacklists` | `data:[{id,name,owner,total}]` |
| List items | `GET /v2/blacklists/{list_id}/items?page=&per_page=` (per_page default/max 5000) | `meta{list_id,page,per_page,total}`, `data:[{type: email\|domain\|linkedin, value}]` |
| Delete list | `DELETE /v2/blacklists/{list_id}` | 204. Cannot delete your last list (422) |

**Analytics and activity** (all Free). Source: [S].

| Purpose | Method + path | Params | Response highlights |
|---|---|---|---|
| Aggregate analytics | `GET /v2/statistics/campaign-analytics` | `date_from`, `date_to` (req, yyyy-mm-dd); `campaign_id` (comma-separated), `sender_email`, `sender_linkedin`, `campaign_owner` | flat object: `emails_sent, delivered, bounced, email_opens, link_clicks, email_replies, unsubscribed, auto_replied, interested, maybe, not_interested`, … each with a `*_rate` **string like "70%"** |
| Progress | `GET /v2/campaigns/{campaign_id}/progress` | | `{status:"Active", unfinished:int, progress:"90%"}` |
| **Recipients activity (best for sync)** | `GET /v2/campaigns/{campaignId}/recipients-activity` | `dateFrom`, `dateTo` (req, Y-m-d UTC, **max 31-day range**), `offset`, `limit` (default 100, max 1000) | `data:[{event_time, event_type, sender_email, email_subject, recipient_email, recipient_name, phone, industry, country, location, company, job_position, hq_phone, website}]`, `pagination{total,offset,limit,has_more}`. `event_type` ∈ `sent, open, click, reply, bounce, unsubscribe` |
| Sent emails | `GET /v1/emails-sent?campaignId=&offset=` | up to 10,000 per request; without `offset` you get the **last** 10,000 | `[{sentDate{date,timezone_type,timezone}, userName, userEmail, campaign, hash, id}]` |
| Opens | `GET /v1/get-emails-opened?campaignId=&offset=` | 10,000 per request | `[{visitedAt{...}, campaignId, campaign, prospectId, prospectFirstName, prospectLastName, prospectName, prospectEmail, emailSubject, emailBody, customFields, customField_<Name>, ...}]` |
| Clicks | `GET /v1/get-emails-clicked?campaignId=&offset=` | same shape as opens | |
| Email replies | `GET /v1/get-emails-replies?campaignId=&offset=` | 10,000 per request | same shape as opens. `emailSubject`/`emailBody` are **the email that was replied to, not the reply text** |
| **All replies (with reply text)** | `GET /v2/campaigns/{campaign_id}/all-replies?offset=` | `offset` only | `data:[{campaignId, campaign, prospectId, prospectName, prospectEmail, linkedInProfile, replyType, receivedAt, replies:[{subject, message, receivedAt}]}]`. `replyType` ∈ `email, linkedinMessage, linkedinInvite, linkedinInMail`. One record per (prospect, reply type) |

**Send an in-thread reply via the API: not available.** [S] has no send-email or reply endpoint; outbound mail only goes through campaign steps. Confirmed by absence only.

**Webhooks.** Source: [S] Webhooks.
- `GET /v2/webhooks`, `POST /v2/webhooks` (JSON `event_object`, `event_action`, `endpoint_url`), `PUT /v2/webhooks/{id}` (`status`: `active`\|`deactivated`), `DELETE /v2/webhooks/{id}`.
- Model: `{id, end_point, event_object, event_action, status, created_at}`. **Note the field names differ**: the request uses `endpoint_url`, the response uses `end_point`.
- Events (object → actions):
  - `campaign_email` → `sent`, `first_sent`, `opened`, `bounced`, `clicked`
  - `campaign_reply` → `received`, `first_received`, `autoreply_received`
  - `campaign_li_reply` → `received`, `first_received`
  - `campaign_li` → `connection_request_accepted`
  - `prospect` → `campaign_finished`, `unsubscribed`, `found_by_li_url`, `found_emails_by_name_by_domain`, `found_company_by_domain`
  - `company` → `found_domains_by_names`, `found_company_by_domain`
  - `email_verification` → `verified`
  - `email` → `found_emails_by_domain`, `found_generic_contacts_by_domain`, `found_prospect_emails`
  - `database_search` → `task_result`
- Limits: "premium plan users can create up to 50 webhooks".
- Success means a 2xx response within **3 s**. Otherwise there are 7 attempts over 38 h (0, +20 min, +40 min, +60 min, +4 h, +8 h, +24 h). After all fail, the **webhook is deactivated**.
- Webhook payload body schema / signature header: **UNCONFIRMED** (not documented on [S]).

### 1.7 Credits, balance

| Call | Cost | Source |
|---|---|---|
| Email verification | 1 credit per `valid`/`not_valid`; `unknown` free | KB email-verification-credits |
| Name+domain finder | 1 credit per email found with `valid` or `unknown` | [S] |
| Domain search start calls | 1 credit per unique request (no charge if no results) | [S] |
| Prospect email reveal | 1 credit per prospect with an email | [S] |
| Count emails, all campaign/list/DNE/analytics/webhook calls | Free | [S] |
| Balance | `GET /v1/get-balance` → `data{balance:"25000.00" (string), teamwork, unique_recipients_used, limit_resets_in (days), expires_in (days)}` | [S] |

### 1.8 Tracking / stop-on-company-reply

These are **not UI-only**. `tracking.open`, `tracking.link_click`, `sending_settings.skip_who_replied` and `sending_settings.skip_company_domain_who_replied` are fields on `POST /v2/campaigns/create` (see 1.6). They can also be set in the UI. [S]

---

## 2. Apify actors

Live data from the public Apify API on 2026-10-23: `GET https://api.apify.com/v2/acts/<user>~<name>` (pricing) and `GET https://api.apify.com/v2/acts/<user>~<name>/builds/default` (input schema + README). Human pages: `https://apify.com/<user>/<name>` and `/input-schema`. **All 8 ids exist and none is deprecated** (`isDeprecated: false`). All are **PAY_PER_EVENT**. Prices are USD per event, by plan tier: FREE / BRONZE (Starter) / GOLD (Business).

### 2.1 Pricing summary

| Actor (id) | Primary event | Price FREE / BRONZE / GOLD | Other events | min `maxTotalChargeUsd` |
|---|---|---|---|---|
| `harvestapi/linkedin-post-search` (`buIWk2uOUzTmcLsuB`) | `post` | 0.002 / 0.002 / 0.0015 | `main-profile` 0.002, `full-profile` 0.004, `reaction` 0.002, `comment` 0.002, **`no-result` 0.001 per 0-result query**, `apify-actor-start` 0.00005 | 0.01 |
| `harvestapi/linkedin-company` (`UwSdACBp7ymaGUJjS`) | `apify-default-dataset-item` (Company) | 0.004 / 0.004 / 0.003 | `apify-actor-start` 0.00005 | 0.004 |
| `harvestapi/linkedin-company-employees` (`Vb6LZkh4EqRlR0Ka9`) | `short-profile` | 0.003 / 0.003 / 0.0015 | `full-profile` 0.008/0.008/0.004; `full-profile-with-email` 0.012/0.012/0.008; **`actor-start` 0.02/0.02/0.015 per run** | 0.05 |
| `harvestapi/linkedin-profile-search` (`M2FMdjRVeF1HPGFcc`) | **`search-page` (up to 25 short profiles) 0.10 / 0.10 / 0.05 per page** | | `full-profile` 0.004/0.004/0.0032; `full-profile-with-email` 0.01/0.01/0.008 | 0.1 |
| `curious_coder/linkedin-jobs-scraper` (`hKByXkMQaC5Qt9UMN`) | `apify-default-dataset-item` | 0.002 / 0.001 / 0.001 | `apify-actor-start` 0.00005 | 0.001 |
| `apidojo/tweet-scraper` (`61RPP7dywgiy0JPD0`) | `apify-default-dataset-item` (tweet) | 0.0004 all tiers ($0.40/1k) | none | null |
| `apify/instagram-scraper` (`shu8hvrXbJbY3Eb9W`) | `result` | 0.0027 / 0.0023 / 0.0015 | none | 0.0027 |
| `apify/google-search-scraper` (`nFJndFXA5zjCTuudP`) | `search-page-scraped` | 0.0045 / 0.0025 / 0.0018 per SERP page | add-ons (AI overview, AI mode, Perplexity, ChatGPT, Copilot, Gemini, leads, email verify, ads, website content); `apify-actor-start` 0.00005 | 0.5 |

Source for all rows: `https://api.apify.com/v2/acts/<id>` → `pricingInfos[-1]` (pricing start dates 2026-02 to 2026-10).

### 2.2 Input schemas and outputs

None of these actors has a schema-level `required` list except google-search-scraper (`queries`). "prefill" is the console's example value, **not a default**. Source: `https://api.apify.com/v2/acts/<id>/builds/default` → `inputSchema`; output fields come from the README example / dataset schema in the same build.

**harvestapi/linkedin-post-search** (build 0.0.114). https://apify.com/harvestapi/linkedin-post-search/input-schema
- Inputs: `searchQueries` [array str], `maxPosts` [int, 0 = all], `postedLimit` [enum `any,1h,24h,week,month,3months,6months,year`], `postedLimitDate` [str ISO], `sortBy` [`relevance`\|`date`], `authorUrls` [array], `authorsCompanies` [array], `mentioningMember` [array], `mentioningCompany` [array], `contentType` [`all,videos,images,jobs,live_videos,documents,collaborative_articles`], `authorsIndustryId` [array], `authorKeywords` [str], `profileScraperMode` [`short`\|`main`, default `short`], `startPage` [int 1–100, default 1], `scrapePages` [int 0–100, 100 posts/page], `scrapeReactions` [bool], `maxReactions` [int], `reactionsProfileScraperMode`, `postNestedReactions`, `scrapeComments` [bool], `commentsPostedLimit`, `maxComments` [int], `commentsProfileScraperMode`, `postNestedComments`.
- Output item: `type, id, linkedinUrl, content, author{universalName|publicIdentifier, name, info, website, avatar{url,...}, linkedinUrl}, postedAt{timestamp, date, postedAgoShort, postedAgoText}, postImages, socialContent{...}, engagement{likes, comments, shares, reactions[]}`.

**harvestapi/linkedin-company** (build 0.0.44)
- Inputs: `companies` [array of LinkedIn company URLs], `searches` [array of company names].
- Output: `id, universalName, linkedinUrl, name, tagline, website, logo, foundedOn{year}, employeeCount, employeeCountRange{start,end}, followerCount, description, companyType, locations[{country, geographicArea, city, line1, headquarter, parsed, postalCode}], specialities, industries, logos, backgroundCovers, phone, fundingData{numFundingRounds, lastFundingRound{fundingType, moneyRaised, announcedOn, leadInvestors,...}, ...}`.

**harvestapi/linkedin-company-employees** (build 0.0.163)
- Inputs: `companies` [array of full LinkedIn company URLs], `profileScraperMode` [enum **`"Short ($4 per 1k)"` \| `"Full ($8 per 1k)"` \| `"Full + email search ($12 per 1k)"`**, default `"Full ($8 per 1k)"`]. These literal strings include the price, and the label prices do not match the event prices in 2.1. Other inputs: `maxItems` [int], `locations`, `searchQuery`, `jobTitles`, `pastJobTitles`, `schools`, `industryIds`, `yearsAtCurrentCompanyIds`, `yearsOfExperienceIds`, `seniorityLevelIds` (`100…320`), `functionIds` (`1…26`), `companyHeadcount` (`A…I`), `recentlyChangedJobs` [bool], `companyBatchMode` [`all_at_once`\|`one_by_one`, default `all_at_once`], `maxItemsPerCompany`, `startPage`, `takePages` (25/page), plus `exclude*` filters.
- Output (full): `id, publicIdentifier, linkedinUrl, firstName, lastName, headline, about, openToWork, hiring, photo, premium, location{linkedinText, countryCode, parsed{...}}, topSkills, connectionsCount, followerCount, currentPosition[{companyName}], experience[{position, companyName, companyLinkedinUrl, companyId, duration, startDate, endDate, description, skills}], education[...], certifications, skills, ...`. An email field is presumably present in "email search" mode, but its exact name is **UNCONFIRMED**.

**harvestapi/linkedin-profile-search** (build 0.0.279)
- Inputs: `profileScraperMode` [enum `"Short"`\|`"Full"`\|`"Full + email search"`, default `"Full"`; **different strings from company-employees**], `searchQuery` [str, LinkedIn operators supported], `maxItems` [int], `currentCompanies`/`pastCompanies` [arrays of **full LinkedIn company URLs**], `currentJobTitles`, `pastJobTitles`, `locations`, `schools`, `industryIds`, `seniorityLevelIds`, `functionIds`, `yearsOfExperienceIds`, `yearsAtCurrentCompanyIds`, `companyHeadcount`, `companyHeadquarterLocations`, `firstNames`, `lastNames`, `profileLanguages`, `recentlyChangedJobs`, `recentlyPostedOnLinkedIn`, `exclude*`, `startPage` [default 1], `takePages` [0–100, 25/page], `autoQuerySegmentation*`, `profileDeduplicationMode` + `mongoDb*` (optional MongoDB dedupe), `searchCoverage` [`medium`\|`high`].
- Cost model: you pay **$0.10 (FREE/BRONZE) per search page of 25** plus a per-profile charge for Full / email modes.
- Output: same profile shape as company-employees (`id, publicIdentifier, linkedinUrl, firstName, lastName, headline, location{...}, currentPosition[...], experience[...], profileTopEducation, ...`).

**curious_coder/linkedin-jobs-scraper** (build 1.7.18)
- Inputs: `urls` [array of public LinkedIn job-search URLs, default `[]`], or (when `urls` is empty) `keywords`, `location`, `geoId`, `distance`, `datePosted` [`anyTime`\|`past24Hours`\|`pastWeek`\|`pastMonth`, default `anyTime`], `companyIds` [array of numeric ids], `under10Applicants` [bool]. Also `autoConvertToAiSearch` [default true], `scrapeCompany` [default **true**, slower], `limitPerSource` [int], `splitByLocation` [default false], `splitCountry`. LinkedIn caps results at 1000 per search URL.
- Output: `id, link, title, companyName, companyLinkedinUrl, companyLogo, location, salaryInfo, postedAt, benefits, descriptionHtml, descriptionText, applicantsCount, applyUrl, jobPosterName, jobPosterTitle, jobPosterPhoto, jobPosterProfileUrl, seniorityLevel, employmentType, jobFunction, industries, companyDescription, companyWebsite, companyEmployeesCount`.

**apidojo/tweet-scraper** ("Tweet Scraper V2", build 0.0.1560)
- Inputs: `startUrls` [array], `searchTerms` [array, X advanced-search syntax], `twitterHandles` [array], `conversationIds` [array], `maxItems` [int; **empty = unlimited**], `sort` [`Top`\|`Latest`\|`Latest + Top`], `tweetLanguage` [ISO 639-1], `onlyVerifiedUsers`, `onlyTwitterBlue`, `onlyImage`, `onlyVideo`, `onlyQuote`, `author`, `inReplyTo`, `mentioning`, `geotaggedNear`, `withinRadius`, `geocode`, `placeObjectId`, `minimumRetweets`, `minimumFavorites`, `minimumReplies`, `start`, `end` [dates], `includeSearchTerms` [bool], `customMapFunction`.
- **README restrictions**: each query must return **≥50 tweets**; **max 1 concurrent run** with up to 5 batched queries; no repeated "monitoring" runs of the same query; no single-tweet or conversation scraping; **Free plan cannot use it via API** (5 demo runs/month, 10 items each). The README points to `apidojo/twitter-scraper-lite` for smaller jobs.
- Output: `type:"tweet", id, url, twitterUrl, text, retweetCount, replyCount, likeCount, quoteCount, bookmarkCount, createdAt ("Fri Nov 24 17:49:36 +0000 2023"), lang, isReply, isRetweet, isQuote, quoteId, card, place, author{userName, name, id, url, isVerified, isBlueVerified, verifiedType, profilePicture, followers, following}, article{...}`.

**apify/instagram-scraper** (build 0.0.830)
- Inputs: `resultsType` [`posts`\|`details`\|`comments`\|`reels`\|`mentions`\|`stories`, default `posts`], `directUrls` [array], `resultsLimit` [int ≥1, per URL], `onlyPostsNewerThan` [date or relative e.g. `"2 months"`], `search` [str], `searchType` [`hashtag`\|`profile`\|`place`\|`user`, default `hashtag`], `searchLimit` [1–250], `addParentData` [default false].
- Output (posts): `id, type, shortCode, caption, hashtags, mentions, url, commentsCount, firstComment, latestComments, displayUrl, images, videoUrl, likesCount, videoViewCount, timestamp, childPosts, ownerUsername, ownerId, ownerFullName, isPinned, productType, inputUrl, ...`. Details mode returns `username, fullName, biography, externalUrl, followersCount, followsCount, isBusinessAccount, businessCategoryName, verified, private, postsCount, latestPosts, ...`.

**apify/google-search-scraper** (build 0.0.468)
- Inputs: **`queries` [string, required, newline-separated terms or Google URLs; a string, not an array]**, `maxPagesPerQuery` [int ≥1, ~10 results/page], `countryCode`, `searchLanguage`, `languageCode`, `locationUule`, `forceExactMatch`, `site`, `relatedToSite`, `wordsInTitle`/`wordsInText`/`wordsInUrl` [arrays], `quickDateRange` (e.g. `d10`), `beforeDate`/`afterDate`, `fileTypes`, `mobileResults`, `includeUnfilteredResults`, `saveHtml`, `saveHtmlToKeyValueStore` [**default true**], `includeIcons`. Paid add-ons (keep these off): `aiOverview`, `aiModeSearch`, `geminiSearch`, `perplexitySearch`, `chatGptSearch`, `copilotSearch`, `maximumLeadsEnrichmentRecords` [default 0], `verifyLeadsEnrichmentEmails`, `linkProspecting`, `websiteContentScraper`, `focusOnPaidAds`.
- Output: **one item per SERP page**: `searchQuery{term, url, device, page, type, domain, countryCode, languageCode, locationUule}, relatedQueries[{title,url}], paidResults[], paidProducts[], organicResults[{title, url, displayedUrl, description, emphasizedKeywords, siteLinks, productInfo, type, position}], peopleAlsoAsk[], aiModeResult?`.

**trudax/reddit-scraper-lite** (build 5.8.7, checked 2026-10-23 via `api.apify.com/v2/acts/trudax~reddit-scraper-lite/builds/default`). Used only to look up the poster of intent-sheet Reddit rows (plan §15.5), because last30days gives no Reddit author and reddit.com blocks free fetching.
- Inputs: `startUrls` [array of `{url}`], `skipComments` / `skipUserPosts` / `skipCommunity` [bool, default false], `maxItems` [int, **default 10**], `maxPostCount` [int, **default 10**: set it, or a 20-post lookup silently stops at 10], `maxComments`, `searches`, `sort`, `time`, `includeNSFW` (default true), `proxy`, ... No required fields.
- Output (post): `id` (`t3_<id>`), `parsedId`, `url`, `username`, `userId`, `title`, `communityName`, `body`, `createdAt`, `dataType: "post"` (comments have `dataType: "comment"`).
- Price: README says 1,000 results for under $4 of platform usage. We budget $0.004 per item, 2 items per post, $0.01 minimum.

### 2.3 apify-client (Python) v3

Latest on PyPI: **3.3.0** (requires Python ≥3.11) (https://pypi.org/pypi/apify-client/json). Signatures below were read from the 3.3.0 wheel source (`apify_client/_resource_clients/*.py`, `_models.py`), which matches https://docs.apify.com/api/client/python/reference/class/ActorClient.md / RunClient.md / UserClient.md / DatasetClient.md.

**v3 breaking changes** (https://docs.apify.com/api/client/python/docs/upgrading/upgrading-to-v3.md):
- Methods return **Pydantic models, not dicts**: use `run.id` and `run.default_dataset_id`, not `run["id"]`.
- `DatasetClient.list_items()` items stay plain dicts.
- Keyword-only arguments.
- Status enums are now string literals.
- Tiered timeouts (`timeout_short`/`medium`/`long`/`max`) are `timedelta`s.
- 404 raises `NotFoundError` on ambiguous endpoints.
- Async `iterate_*` are no longer coroutine functions.

| Task | Code | Notes |
|---|---|---|
| Client | `ApifyClient(token, *, api_url=..., max_retries=..., min_delay_between_retries=timedelta, timeout_short/medium/long/max=timedelta, headers=...)` | `_apify_client.py` |
| Start (non-blocking, get run id first) | `run = client.actor("harvestapi/linkedin-post-search").start(run_input={...}, max_items=100, max_total_charge_usd=Decimal("0.50"), memory_mbytes=..., run_timeout=timedelta(...), wait_for_finish=None)` → `Run`; `run.id` | kwargs map to API query params `maxItems`, `maxTotalChargeUsd`, `waitForFinish`. **`max_total_charge_usd` is typed `Decimal`** |
| Call (blocking) | `.call(run_input=..., max_items=..., max_total_charge_usd=..., wait_duration=timedelta \| None, logger=None, ...)` → `Run \| None` | v3 uses `wait_duration` (timedelta) and `run_timeout`. The v2 names `wait_secs`/`timeout_secs` are gone (compare signatures) |
| Wait / resume by id | `client.run(run_id).wait_for_finish(wait_duration=timedelta(minutes=10))` → `Run \| None`. If `run.status` is not terminal (`SUCCEEDED`, `FAILED`, `TIMED-OUT`, `ABORTED`), it is still running | `run.py` |
| Poll | `client.run(run_id).get()` → `Run \| None`; also `.abort(gracefully=...)`, `.resurrect()` | |
| Statuses | `READY, RUNNING, SUCCEEDED, FAILED, TIMING-OUT, TIMED-OUT, ABORTING, ABORTED` | `_literals.py` |
| Dataset items | `client.dataset(run.default_dataset_id).list_items(offset=, limit=, clean=, fields=, ...)` → `DatasetItemsPage{items, total, offset, count, limit, desc}`; or `.iterate_items(...)`; or `client.run(id).iterate_dataset_items(...)` (streams while the run is still going) | `dataset.py`, `run.py` |
| Run cost | `run.usage_total_usd` (float \| None): "Total cost in USD for this run. Represents what you actually pay… For run owners: includes platform usage and/or event costs depending on the Actor's pricing model." Also `run.charged_event_counts` (`dict[str,int]`, e.g. `{"post": 37, "apify-actor-start": 1}`) and `run.pricing_info`. `run.usage_usd` is **absent for standard pay-per-event actors** | `_models.py` Run |
| Account usage | `client.user().monthly_usage()` → `GET /v2/users/me/usage/monthly[?date=YYYY-MM-DD]`: `usageCycle{startAt,endAt}`, `monthlyServiceUsage{<SERVICE>: {quantity, baseAmountUsd, baseUnitPriceUsd, amountAfterVolumeDiscountUsd, priceTiers}}`, `dailyServiceUsages[{date, serviceUsage, totalUsageCreditsUsd}]`, `totalUsageCreditsUsdBeforeVolumeDiscount`, `totalUsageCreditsUsdAfterVolumeDiscount` | https://docs.apify.com/api/v2/users-me-usage-monthly-get.md |
| Account limits / spend so far | `client.user().limits()` → `GET /v2/users/me/limits`: `monthlyUsageCycle{startAt,endAt}`, `limits{maxMonthlyUsageUsd, maxMonthlyActorComputeUnits, maxConcurrentActorJobs, dataRetentionDays, ...}`, `current{monthlyUsageUsd, monthlyActorComputeUnits, ...}` | https://docs.apify.com/api/v2/users-me-limits-get.md |
| Max-charge semantics | "The Actor terminates gracefully when it reaches that limit… you are never charged for produced events over the defined limit" | https://docs.apify.com/actors/running/actors-in-store.md |

Whether `users/me/usage/monthly` breaks out pay-per-event actor charges under a specific service key, and the key name, are **UNCONFIRMED**. The example response's `monthlyServiceUsage` is empty `{}`. For "money spent this month", use `limits().current.monthly_usage_usd`.

---

## 3. last30days skill (mvanhorn/last30days-skill)

Read from the v3.30.0 tarball (https://github.com/mvanhorn/last30days-skill/archive/refs/tags/v3.30.0.tar.gz). Nothing was executed.

| Fact | Value | Source |
|---|---|---|
| Latest release | **v3.30.0**, published 2026-10-22 (prior: v3.29.0, v3.28.1) | `gh api repos/mvanhorn/last30days-skill/releases/latest` |
| Engine script | `skills/last30days/scripts/last30days.py` (package `skills/last30days/scripts/lib/`) | repo tree @v3.30.0 |
| Python | **≥3.12** (`requires-python = ">=3.12"`). **Runtime deps: none** (`dependencies = []`; pytest etc. only in the dev group). Optional external bins: `yt-dlp` (YouTube), `node` (vendored Bird X client), `*-pp-cli` tools | `pyproject.toml`; `skills/last30days/references/runtime.md`; README.md |
| JSON output | `--emit=json` (choices: `compact` (default), `json`, `context`, `md`, `html`, `brief`); `--json-profile=agent` (default, versioned) \| `raw` (unversioned internal dump); `--output PATH` to write a file | `last30days.py` `build_parser()`; `docs/reference/json-export.md` |
| Source selection | `--search reddit,hackernews,x,...` (comma list). Valid names: `reddit, x, youtube, tiktok, instagram, hackernews, bluesky, truthsocial, polymarket, grounding, xiaohongshu, github, perplexity, threads, pinterest, digg, arxiv, techmeme, trustpilot, amazon, meta_ads, jobs, linkedin, corpus, dripstack, telegram, devto`. Aliases: `hn`→hackernews, `web`→grounding, `bsky`, `truth`, `xhs`, `meta`/`meta-ads`, `dev.to`, `xquik`→x. Unknown names cause `SystemExit` | `lib/pipeline.py` `SEARCH_ALIAS`, `MOCK_AVAILABLE_SOURCES`; `last30days.py` `parse_search_flag` |
| Other flags | `--quick` / `--deep`, `--days N` (alias `--lookback-days`, default 30), `--as-of YYYY-MM-DD`, `--subreddits a,b`, `--max-results`, `--max-per-source`, `--plan <json\|path>`, `--mock`, `--diagnose`, `--store`, `--debug` | `build_parser()` |
| Env / config | Reads `~/.config/last30days/.env` (override `LAST30DAYS_CONFIG_DIR`); `LAST30DAYS_DEFAULT_SEARCH`, `INCLUDE_SOURCES`, `EXCLUDE_SOURCES` | CONFIGURATION.md |
| **Keyless sources** | Reddit (public, default free path; paced 1 req/s via `LAST30DAYS_REDDIT_KEYLESS_RATE`), Hacker News (Algolia `hn.algolia.com/api/v1`), Polymarket, GitHub (`GITHUB_TOKEN` optional), dev.to/arXiv/Techmeme (opt-in or CLI) | CONFIGURATION.md "Source requirements"; `lib/hackernews.py` |
| Keyed sources | X: one of `X_BEARER_TOKEN` (+`LAST30DAYS_X_BACKEND=xapi`), `AUTH_TOKEN`+`CT0`, `XAI_API_KEY`, `XQUIK_API_KEY`, `FROM_BROWSER`. TikTok/Instagram/Threads/Pinterest/LinkedIn/Meta Ads/Telegram: `SCRAPECREATORS_API_KEY`. Reddit backfill: `SCRAPECREATORS_API_KEY` (optional). Bluesky: `BSKY_HANDLE`+`BSKY_APP_PASSWORD`. Web: `BRAVE_API_KEY`\|`EXA_API_KEY`\|`SERPER_API_KEY`\|`PARALLEL_API_KEY`. Perplexity: `PERPLEXITY_API_KEY`/`OPENROUTER_API_KEY`. YouTube: `yt-dlp` bin | CONFIGURATION.md |
| Reasoning provider (planner/rerank) | auto: `GOOGLE_API_KEY`/`GEMINI_API_KEY` → `OPENAI_API_KEY` → `XAI_API_KEY` → `OPENROUTER_API_KEY` → local deterministic fallback. **If any of these keys is set, topic + snippets are sent to that provider for reranking even with `--plan`** | CONFIGURATION.md "Reasoning provider priority" |
| **Agent-host gate** | If the process env has `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CODEX_*`, `LAST30DAYS_HOST` or `LAST30DAYS_HOST_AGENT=1`, a research run **without `--plan` exits 2** (`[Planner] LAW 7`). Bypass: pass `--plan`, or set `LAST30DAYS_ALLOW_ENGINE_PLAN=1` in the process env | CONFIGURATION.md "Agent-hosted runs must pass --plan" |
| `--plan` shape | `{"intent": str, "freshness_mode": str, "cluster_mode": str, "subqueries": [{"search_query": str, "ranking_query": str, "sources": [str,...], "weight"?: num, "label"?: str}], "source_weights"?: {src: num}}`. All 4 top-level keys are required | `lib/planner.py` `validate_external_plan` |
| Remote mode caveat | With `LAST30DAYS_API_KEY`+`LAST30DAYS_API_BASE` set, `--json-profile=agent` exits 2 | json-export.md |

**Agent JSON schema (`schema_version` "1.4").** Source: https://raw.githubusercontent.com/mvanhorn/last30days-skill/v3.30.0/docs/reference/json-export.md
- Top level (always present): `schema_version, query, generated_at (RFC 3339 UTC), window_days, source_status{source: state}, freshness_verdicts[], clusters[], results[], usage (obj|null)`.
- `source_status` states: `ok, no-results, partial, rate-limited, auth-failed, payment-required, unreachable, timeout, schema-drift, skipped-unconfigured, error`. Only `no-results` means a clean zero.
- `clusters[]`: `title, summary, sources[], engagement_total`.
- `results[]`: `candidate_id, title, source, url (may be ""), published_at (omitted if unknown), summary, engagement{native counters, e.g. Reddit score/num_comments/upvote_ratio; HN points/num_comments}, relevance_score (0–1), cluster (index; omitted if none)`. Unknown values are **omitted, not null**.
- **The agent profile has no `author` or full `text`/`body` field.** Those exist only in `--json-profile=raw` (internal `SourceItem`: `item_id, source, title, body, url, author, container, published_at, date_confidence, engagement, snippet, metadata, …`), which is unversioned. Source: `lib/schema.py`.
- Comparison runs use an envelope: `{schema_version, comparison:true, entities[], reports:[{entity, report}]}`.

---

## UNCONFIRMED list
1. Snov: 429 response body / `Retry-After` header on rate-limit.
2. Snov: whether v1 endpoints accept the Bearer header (docs samples use an `access_token` param).
3. Snov: webhook payload JSON schema and any signature/verification header.
4. Snov: meaning of the numeric `smtpStatus` in `/v1/prospect-list`.
5. Snov: no create-custom-field, delete-prospect, or send/reply-email endpoint. Inferred from absence in the API reference.
6. Snov: whether `PATCH /v2/campaigns/{id}` can toggle tracking/skip settings on a running campaign.
7. Snov: custom-field limit wording conflict (KB says "30 per list" and "up to 30" overall).
8. Apify: exact email field name in harvestapi "Full + email search" output.
9. Apify: service key for pay-per-event charges in `/v2/users/me/usage/monthly`.
