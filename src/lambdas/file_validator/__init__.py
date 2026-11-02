"""Lambda `file_validator`: checks every AP file the moment it lands in S3 (S3 event → Lambda).

- validation.py  pure checks on the file name and content (no AWS): what is wrong, and where
- storage.py     all S3 access: read the file, look for an earlier copy, copy or quarantine it
- notifier.py    the rejection email to AP (SNS)
- handler.py     the Lambda entry point: one decision per file (validated, duplicate, rejected)
"""
