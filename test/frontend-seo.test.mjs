import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import { test } from "node:test";

const FRONTEND = new URL("../frontend/", import.meta.url);

async function text(path) {
  return readFile(new URL(path, FRONTEND), "utf8");
}

function jsonLd(html) {
  return [...html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)].map(
    (match) => JSON.parse(match[1]),
  );
}

test("homepage publishes canonical brand and social metadata", async () => {
  const html = await text("index.html");

  assert.match(html, /<link rel="canonical" href="https:\/\/www\.crowdcode\.app\/" \/>/);
  assert.match(html, /<meta property="og:site_name" content="CrowdCode" \/>/);
  assert.match(html, /<meta property="og:image" content="https:\/\/www\.crowdcode\.app\/social-card\.png" \/>/);
  assert.doesNotMatch(html, /href="data:image\//);

  const graph = jsonLd(html)[0]["@graph"];
  assert.equal(graph.find((node) => node["@type"] === "WebSite").name, "CrowdCode");
  assert.equal(
    graph.find((node) => node["@type"] === "Organization").logo.url,
    "https://www.crowdcode.app/icon-512.png",
  );
});

test("favicon and share assets are real deployable files", async () => {
  const assets = [
    "favicon.svg",
    "favicon-96x96.png",
    "favicon.ico",
    "apple-touch-icon.png",
    "icon-192.png",
    "icon-512.png",
    "social-card.png",
    "site.webmanifest",
  ];

  await Promise.all(assets.map((asset) => access(new URL(asset, FRONTEND))));
});

test("informational content has a crawlable URL", async () => {
  const home = await text("index.html");
  const explainer = await text("how-it-works/index.html");

  assert.match(home, /<a href="\/how-it-works">\[How it works\]<\/a>/);
  assert.match(
    explainer,
    /<link rel="canonical" href="https:\/\/www\.crowdcode\.app\/how-it-works" \/>/,
  );
  assert.equal(jsonLd(explainer)[0]["@type"], "WebPage");
});

test("robots and sitemap expose every canonical page", async () => {
  const robots = await text("robots.txt");
  const sitemap = await text("sitemap.xml");

  assert.match(robots, /^User-agent: \*$/m);
  assert.match(robots, /^Allow: \/$/m);
  assert.match(robots, /Sitemap: https:\/\/www\.crowdcode\.app\/sitemap\.xml/);
  assert.match(sitemap, /<loc>https:\/\/www\.crowdcode\.app\/<\/loc>/);
  assert.match(sitemap, /<loc>https:\/\/www\.crowdcode\.app\/how-it-works<\/loc>/);
});
