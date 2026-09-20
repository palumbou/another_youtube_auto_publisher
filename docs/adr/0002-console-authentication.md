# ADR 0002 — Console behind Cognito + API Gateway JWT authorizer + WAF IP set

Date: 2026-09-20. Status: accepted.

## Context

The previous console was a Lambda Function URL protected by a shared token in the link. The specification requires an IP allowlist and a personal login, no shared password, MFA where supported, and configuration of allowed IPs as deployment parameters.

## Decision

- Identity: a Cognito user pool with exactly one admin-created user (the owner), email username, software-token MFA available, no self sign-up.
- Transport: an HTTP API with a JWT authorizer bound to that pool; the Lambda receives only verified claims and denies any request without them (`GatewayJwtAuthenticator`).
- Network: a regional WAF web ACL with a default block and two allow rules from IPv4/IPv6 IP sets filled from the `review_ip_allowlist` variable; the application checks the same list again and fails closed when it is empty.
- Local: `LocalAuthenticator` exists only when `LOCAL_MODE=1` and `ENVIRONMENT=local`; settings refuse any other combination.

## Alternatives rejected

- Verifying Cognito JWTs inside the Lambda: needs a cryptography dependency and key caching; the gateway already does it.
- CloudFront + Lambda@Edge: more moving parts for a single-user console.
- Keeping the shared token: no personal identity, no MFA, token in browser history and Terraform output.
