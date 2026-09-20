# Cost estimate — per 10-minute master video

Date: 2026-09-20. Region assumed: eu-west-1 (the July 2026 stack was applied there).

## Verification status of prices

| Price | Source checked on 2026-09-20 | Status |
|---|---|---|
| Fargate Linux/x86 CPU, US East example on the pricing page | https://aws.amazon.com/fargate/pricing/ — `$0.000011244 per vCPU second` (≈ $0.04048/vCPU-hour) | verified for **US East (N. Virginia)** only; the Ireland table is rendered client-side and was **not** readable |
| Fargate memory and ephemeral storage | same page | **not verified** (values not extractable) |
| Amazon Transcribe standard batch, tier 1 | https://aws.amazon.com/transcribe/pricing/ | **not verified** (region tables rendered client-side); free tier verified: 60 min/month for 12 months |
| Bedrock, Claude Sonnet family | https://aws.amazon.com/bedrock/pricing/ — the page lists Claude 3.5 Sonnet at `$6.00 / 1M input`, `$30.00 / 1M output` (public extended access, Ireland among the regions) | verified only for the **3.5 Sonnet** line; the configured `model_id` (Sonnet 4.5 inference profile) was **not** on the page |
| Lambda, S3, DynamoDB, SQS, EventBridge, WAF, Cognito, API Gateway | not fetched | **not verified**; assumed negligible at pilot volume, order of a few USD/month fixed (WAF web ACL ≈ $5/month plus rules is the main fixed line) |

Nothing below should be quoted as a price to a third party without re-checking the AWS Pricing Calculator.

## Consumption model (measured shapes, not prices)

For one 10-minute 1080p master with a manifest and up to 4 Shorts:

| Stage | Resource | Measured / assumed quantity |
|---|---|---|
| Ingest | Lambda 512 MB | < 5 s (hash of a ~1 GB object streamed from S3: bounded by S3 throughput; probe-less mode) |
| Processing task | Fargate 2 vCPU / 8 GB / 50 GB ephemeral | local reference: the e2e run rendered a 42 s 1080p master and one Short in **41.7 s wall clock** on this workstation (`docs/evidence/e2e-local-run.*`), including two loudness/cropdetect verification passes. Extrapolated linearly: ≈ 10 min of task time for a 10-minute master with 4 Shorts, i.e. ≈ 0.33 vCPU-hour and ≈ 1.3 GB-hour. |
| Transcript | Transcribe batch | 10 audio minutes |
| Analysis | Bedrock Converse | one call: ≈ 8–15k input tokens (manifest + transcript), ≈ 1.5k output tokens (schema-bound tool call) |
| Storage | S3 | source ≈ 1 GB kept until the owner deletes it; work/ artifacts < 5 MB, expire after 30 days; ready/ Shorts ≈ 4 × 20 MB, expire after 90 days |
| Publish | Lambda 1 GB | one invocation per due schedule, streaming the asset |

## Arithmetic with the verified US-East Fargate CPU price only

- CPU: 0.33 vCPU-hour × $0.04048 ≈ **$0.013**
- Memory, storage, Transcribe, Bedrock: **not computed** (prices not verified above). Using the verified 3.5 Sonnet line as an upper-bound placeholder: 15k × $6/1M + 1.5k × $30/1M ≈ $0.135 per analysis; the actual model configured may differ.

Order of magnitude per 10-minute video, excluding fixed monthly items: **well under $1**, dominated by transcription and the model call. Fixed monthly items to confirm in the calculator: WAF web ACL, Secrets Manager secret (≈ $0.40/month per secret), CloudWatch log ingestion, ECR storage (last five images), DynamoDB PITR.

## Existing July 2026 stack

The state file found in copy A shows a stack applied on 2026-07-05 in eu-west-1 (ECS cluster, ECR repository, four Lambdas, S3 bucket, DynamoDB table, log groups). Without credentials it could not be checked whether it still exists. If it does, ECR image storage and log retention are the ongoing lines; the owner should run `tofu state list` from copy A's `infra/` or check the console before applying this new stack, which reuses several resource names.
