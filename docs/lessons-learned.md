# Lessons learned

Decisions that changed because of evidence, while building this project. Each entry: what we assumed, what the data showed, what we changed, and the measured result.

## 1. An exact amount match is weaker evidence than it looks
- **Assumption:** a resubmitted invoice can be recognized as "same supplier, same exact amount, another number". A window of a month seemed generous and safe.
- **What the data showed:** a test that widens the window from 7 to 31 days, meant to prove that monthly subscriptions are a trap, also raised a false alarm nobody had planted: **two different freight invoices of exactly $4,700.22**, a few weeks apart. With hundreds of invoices from one carrier, a match to the cent happens by chance.
- **Why it matters:** a false duplicate means an analyst chasing a supplier for money it doesn't owe. That damages the relationship and wastes time.
- **Change:** the resubmission rule stays **MEDIUM** confidence (review before acting) and uses a short window (7 days, `detection.resubmission_window_days`). A test now documents both kinds of false alarm a wide window causes (ADR-006).
- **Result:** with 7 days, all 8 planted resubmissions are found with 0 false alarms. With 31 days, the same 8 are found plus the subscriptions and the chance match.
- **Takeaway:** a rule that matches on one number (an amount) has a chance-collision rate that grows with volume. Measure it on realistic data before trusting the rule, and label the confidence honestly.

## 2. A low account limit was absorbed by asynchronous invocation
- **Assumption:** uploading the 128 history files at once would run the validator 128 times in parallel.
- **What the real deploy showed:** **259 throttles**. This account's Lambda concurrency limit is **10**, the reduced default for new or little-used accounts (usually it is 1,000). Only 10 validations could run at a time.
- **Why nothing broke:** S3 invokes Lambda **asynchronously**. A throttled event is not an error returned to S3: Lambda keeps it in its internal queue and retries it with backoff for up to 6 hours. The Monitor tab showed 132 async events received, **0 dropped**, and an oldest wait of 25.6 s. All 128 files were validated with 0 errors.
- **Change:** nothing in the code; the design already handled it. Documented, and the console screenshot is kept (`docs/images/console/console-lambda-monitor-throttles.png`).
- **Takeaway:** check the account's concurrency limit (`aws lambda get-account-settings`) before a bulk load.
  - Asynchronous triggers (S3, SNS, EventBridge) turn a throttle into a delay.
  - Synchronous callers (API Gateway, direct invokes) would have received errors.
  - For a guaranteed ceiling, set **reserved concurrency** on the function, or put an **SQS queue** in between.

## 3. Email is a notification channel, not a delivery guarantee
- **Assumption:** an SNS email subscription delivers every alert.
- **What the real deploy showed:** the duplicate-payment email of the demo day never reached the mailbox, in any folder.
  - The job logged one message sent. SNS reported 0 failed deliveries, and the other email (file rejected) arrived.
  - Two test publishes, one with exactly the same subject and body and one with neutral text, both arrived. Content, filters and permissions were therefore fine; the loss happened between SNS and the mail provider.
  - For email, SNS only confirms the hand-off to the provider, so there is no record of what happened next.
- **Why it matters:** an AP team that relies on the email alone would never know about that duplicate payment.
- **Change:**
  - The source of truth stays durable: the duplicate is in the gold table and in the Athena review list (`sql/02_duplicate_review_list.sql`), which AP checks daily. The email is the nudge.
  - Next step for production: subscribe a durable consumer to the same topic, such as an **SQS queue** feeding a ticketing system. SQS keeps every message until it is processed, and its delivery can be monitored.
- **Takeaway:** for alerts about money, never let a best-effort channel be the only record. Publish to something durable, and treat email as a convenience on top.
