import { mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { OwnerApi } from "../src/agents/client.js";
import { ApiError } from "../src/agents/client.js";
import { downloadFile, planDest, safeName, uniquePath } from "../src/agents/files.js";

// Expected values computed with plugins/hermes/files.py safe_name.
const SAFE_NAMES: [string, string][] = [
  ["report.md", "report.md"],
  ["../../outside/notes.txt", "notes.txt"],
  ["a\\b\\c.txt", "c.txt"],
  ["  .hidden.  ", "hidden"],
  ["..", "file"],
  ["", "file"],
  ["con", "_con"],
  ["CON.txt", "_CON.txt"],
  ["lpt1.log", "_lpt1.log"],
  ["aux.tar.gz", "aux.tar.gz"],
  ["nul.", "_nul"],
  ["we<i>rd:na|me?.md", "we_i_rd_na_me_.md"],
  ["x".repeat(250) + ".txt", "x".repeat(196) + ".txt"],
  ["é".repeat(150) + ".markdown", "é".repeat(95) + ".markdown"],
  ["é.txt", "é.txt"],
  ["a\x00b\x1fc\x7f.txt", "a_b_c_.txt"],
  ["　 spaced 　", "spaced"],
  ["﻿foo", "﻿foo"],
  ["\x85nel\x85", "nel"],
  ["...", "file"],
  ["file.", "file"],
  ["名前".repeat(80) + ".pdf", "名前".repeat(32) + "名.pdf"],
  ["y".repeat(300) + ".averyveryverylongextension", "y".repeat(200)],
  ["😀".repeat(60) + ".png", "😀".repeat(49) + ".png"],
  ["dir/", "file"],
  [".env", "env"],
  ["-rf", "-rf"],
];

function fakeApi(chunks: (Uint8Array | Error)[]): OwnerApi {
  return {
    wallet: null,
    baseUrl: "http://127.0.0.1",
    signed: async () => ({}),
    signedStream: async () =>
      (async function* () {
        for (const chunk of chunks) {
          if (chunk instanceof Error) throw chunk;
          yield chunk;
        }
      })(),
  };
}

describe("file names", () => {
  it("sanitizes exactly like the Hermes plugin's safe_name", () => {
    for (const [input, expected] of SAFE_NAMES) expect(safeName(input), JSON.stringify(input)).toBe(expected);
  });

  it("uses the fallback for empty names", () => {
    expect(safeName(null, "agent")).toBe("agent");
    expect(safeName("../..", "agent")).toBe("agent");
  });
});

describe("never overwriting", () => {
  it("adds -1, -2… and leaves existing files untouched", async () => {
    const dir = await mkdtemp(join(tmpdir(), "cc-unique-"));
    await writeFile(join(dir, "report.md"), "first");
    const a = await uniquePath(dir, "report.md");
    await a.handle.close();
    const b = await uniquePath(dir, "report.md");
    await b.handle.close();
    const c = await uniquePath(dir, "README");
    await c.handle.close();
    expect(a.path).toBe(join(dir, "report-1.md"));
    expect(b.path).toBe(join(dir, "report-2.md"));
    expect(c.path).toBe(join(dir, "README"));
    expect(await readFile(join(dir, "report.md"), "utf8")).toBe("first");
    const d = await uniquePath(dir, ".bashrc");
    await d.handle.close();
    const e = await uniquePath(dir, ".bashrc");
    await e.handle.close();
    expect(e.path).toBe(join(dir, ".bashrc-1"));
  });

  it("downloads twice into two files", async () => {
    const dir = await mkdtemp(join(tmpdir(), "cc-dl-"));
    const file = { id: "fl_1", name: "../../etc/passwd" };
    const one = await downloadFile(fakeApi([Buffer.from("a")]), "ag_1", file, dir, 1000);
    const two = await downloadFile(fakeApi([Buffer.from("b")]), "ag_1", file, dir, 1000);
    expect(one).toEqual({ id: "fl_1", name: "passwd", path: join(dir, "passwd"), size_bytes: 1 });
    expect(two.path).toBe(join(dir, "passwd-1"));
    expect(await readFile(one.path, "utf8")).toBe("a");
    expect(await readFile(two.path, "utf8")).toBe("b");
  });
});

describe("download limits and failures", () => {
  it("refuses a file over the cap and leaves nothing behind", async () => {
    const dir = await mkdtemp(join(tmpdir(), "cc-cap-"));
    const api = fakeApi([new Uint8Array(600_000), new Uint8Array(600_000)]);
    await expect(downloadFile(api, "ag_1", { id: "fl_1", name: "big.bin" }, dir, 1_000_000)).rejects.toThrow(
      "big.bin is larger than the 1 MB download limit",
    );
    expect(await readdir(dir)).toEqual([]);
  });

  it("refuses up front when the listing says the file is too big", async () => {
    const dir = await mkdtemp(join(tmpdir(), "cc-cap-"));
    await expect(
      downloadFile(fakeApi([]), "ag_1", { id: "fl_1", name: "big.bin", size_bytes: 2_000_000 }, dir, 1_000_000),
    ).rejects.toThrow("larger than the 1 MB download limit");
    expect(await readdir(dir)).toEqual([]);
  });

  it("deletes the partial file when the stream fails", async () => {
    const dir = await mkdtemp(join(tmpdir(), "cc-fail-"));
    const api = fakeApi([Buffer.from("half"), new Error("socket hang up")]);
    await expect(downloadFile(api, "ag_1", { id: "fl_1", name: "x.txt" }, dir, 1000)).rejects.toThrow("socket hang up");
    const refused: OwnerApi = { ...fakeApi([]), signedStream: async () => Promise.reject(new ApiError(404, "not_found")) };
    await expect(downloadFile(refused, "ag_1", { id: "fl_2", name: "y.txt" }, dir, 1000)).rejects.toThrow("404 not_found");
    expect(await readdir(dir)).toEqual([]);
  });
});

describe("destination folder", () => {
  it("defaults to ./crowdcode-files/<agent>/ and resolves dest against the working directory", () => {
    expect(planDest("", "../My Agent", "/work")).toBe("/work/crowdcode-files/My Agent");
    expect(planDest("", "", "/work")).toBe("/work/crowdcode-files/agent");
    expect(planDest("out/files", "scout", "/work")).toBe("/work/out/files");
    expect(planDest("/tmp/elsewhere", "scout", "/work")).toBe("/tmp/elsewhere");
  });
});
