# Coval Examples

[![Docs](https://img.shields.io/badge/Documentation-blue)](https://docs.coval.dev/api-reference/v1/introduction)

<p align="center">
  <img src="coval-logo.png" alt="Coval Logo" width="200">
</p>

SDKs and examples for integrating with Coval's API.

## Examples

### TypeScript SDK

Typed TypeScript client generated from Coval's OpenAPI specs with a small
hand-written wrapper for auth, retries, pagination, and typed errors.

```bash
npm install @coval/sdk
```

[View TypeScript SDK](./typescript-sdk)

### Python SDK

Generated Python client for Coval's OpenAPI specs. This package is currently
demo-grade and intentionally lighter than the TypeScript SDK wrapper. It is
published in parity with `@coval/sdk` for customers using Python.

```bash
pip install coval-sdk

# For latest main before the next PyPI release:
# pip install "git+https://github.com/coval-ai/coval-examples.git#subdirectory=python-sdk"
```

[View Python SDK](./python-sdk)

### Upload Conversations

[View upload-conversations examples](./upload-conversations)

### Launch Runs

[View launch-runs examples](./launch-runs)

## Documentation

- [API Reference](https://docs.coval.dev/api-reference)
- [Getting Started](https://docs.coval.dev/getting-started)

## Support

Contact: support@coval.dev
