# =============================================================================
# 3_alerts.tf — the AP mailing list: one SNS topic, one filtered email subscription per person.
#
# Two kinds of email are published to it, each tagged with message attributes `kind` and `entity`:
#   FILE_REJECTED       from the file_validator Lambda (a file must be re-exported)
#   DUPLICATE_PAYMENT   from the Glue job (high-confidence duplicates of the day)
# Each person's filter policy (config/subscribers.yaml) decides which of them reach them.
# =============================================================================

# The topic both publishers send to
resource "aws_sns_topic" "alerts" {
  name = "${local.project}-alerts"
}

locals {
  # One entry per person, keyed "<position>-<role>" (stable and readable)
  subscriptions = { for index, subscriber in local.subscribers : "${index}-${subscriber.role}" => subscriber }

  # Each person's filter policy from their optional `alerts` and `entities` lists. An empty list adds
  # no condition; an empty policy means "no filter" (the person receives every email).
  subscription_filters = {
    for key, subscriber in local.subscriptions : key => merge(
      length(subscriber.alerts) > 0 ? { kind = subscriber.alerts } : {},
      length(subscriber.entities) > 0 ? { entity = subscriber.entities } : {},
    )
  }
}

# One email subscription per person. Each address gets a confirmation email first: nothing
# arrives until the link in it is clicked.
resource "aws_sns_topic_subscription" "email" {
  for_each = local.subscriptions

  topic_arn     = aws_sns_topic.alerts.arn
  protocol      = "email"
  endpoint      = each.value.email
  filter_policy = length(local.subscription_filters[each.key]) == 0 ? null : jsonencode(local.subscription_filters[each.key])
}
