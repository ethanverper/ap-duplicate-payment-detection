# Lessons learned

Decisions that changed because of evidence, while building this project. Each entry: what we assumed, what the data showed, what we changed, and the measured result.

## 1. An exact amount match is weaker evidence than it looks
- **Assumption:** a resubmitted invoice can be recognized as "same supplier, same exact amount, another number". A window of a month seemed generous and safe.
- **What the data showed:** a test that widens the window from 7 to 31 days, meant to prove that monthly subscriptions are a trap, also raised a false alarm nobody had planted: **two different freight invoices of exactly $4,700.22**, a few weeks apart. With hundreds of invoices from one carrier, a match to the cent happens by chance.
- **Why it matters:** a false duplicate means an analyst chasing a supplier for money it doesn't owe. That damages the relationship and wastes time.
- **Change:** the resubmission rule stays **MEDIUM** confidence (review before acting) and uses a short window (7 days, `detection.resubmission_window_days`). A test now documents both kinds of false alarm a wide window causes (ADR-006).
- **Result:** with 7 days, all 8 planted resubmissions are found with 0 false alarms. With 31 days, the same 8 are found plus the subscriptions and the chance match.
- **Takeaway:** a rule that matches on one number (an amount) has a chance-collision rate that grows with volume. Measure it on realistic data before trusting the rule, and label the confidence honestly.
