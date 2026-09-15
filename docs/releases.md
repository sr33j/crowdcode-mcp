# npm releases

The public `sr33j/crowdcode-mcp` repository publishes the `crowdcode-mcp` npm
package through `.github/workflows/release.yml`. npm trusts that workflow
through GitHub OIDC; no `NPM_TOKEN` secret or interactive npm login is required
in CI. The backend and website have separate deployment lifecycles.

1. Run `npm version <version> --workspace crowdcode-mcp --no-git-tag-version`.
2. Match the version in `packages/mcp/src/server.ts` and
   `plugins/crowdcode/.codex-plugin/plugin.json`, and update
   `packages/mcp/CHANGELOG.md`.
3. Run `npm ci`, `npm run build -w @crowdcode/redaction`,
   `npm run typecheck -w @crowdcode/redaction -w crowdcode-mcp`, `npm test`,
   and `npm run check:assets -w crowdcode-mcp`.
4. Commit and push the release changes to the public repository.
5. Create a GitHub Release with tag `v<version>` targeting that commit.
   The workflow requires an exact match between the tag and package version;
   prereleases are skipped. It validates and publishes to npm's `latest` tag.
6. Verify the Release action succeeded and `npm view crowdcode-mcp version`
   reports the new version. Update any parent repository's submodule pointer.

The existing `release-mcpb.yml` workflow builds plugin/MCPB artifacts when a
version tag is pushed. `@crowdcode/redaction` is published separately only
when that package changes.
