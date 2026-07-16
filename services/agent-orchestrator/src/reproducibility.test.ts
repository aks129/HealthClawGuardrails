import fs from "fs";
import path from "path";

const serviceRoot = path.resolve(__dirname, "..");
const packageJson = JSON.parse(
  fs.readFileSync(path.join(serviceRoot, "package.json"), "utf8")
) as {
  version: string;
  devDependencies: Record<string, string>;
};
const packageLock = JSON.parse(
  fs.readFileSync(path.join(serviceRoot, "package-lock.json"), "utf8")
) as {
  packages: Record<
    string,
    { version?: string; devDependencies?: Record<string, string> }
  >;
};

function declaredMajor(range: string): number {
  const match = range.match(/\d+/);
  if (!match) throw new Error(`No major version in ${range}`);
  return Number(match[0]);
}

describe("agent-orchestrator reproducibility", () => {
  it("uses the lockfile and npm ci in the production image", () => {
    const dockerfile = fs.readFileSync(
      path.join(serviceRoot, "Dockerfile"),
      "utf8"
    );

    expect(dockerfile).toContain("COPY package.json package-lock.json ./");
    expect(dockerfile).toMatch(/RUN npm ci\b/);
    expect(dockerfile).not.toMatch(/RUN npm install\b/);
  });

  it("runs the built container with production startup safeguards enabled", () => {
    const dockerfile = fs.readFileSync(
      path.join(serviceRoot, "Dockerfile"),
      "utf8"
    );

    expect(dockerfile).toContain("ENV NODE_ENV=production");
    expect(dockerfile.indexOf("ENV NODE_ENV=production")).toBeGreaterThan(
      dockerfile.indexOf("RUN npx tsc")
    );
  });

  it("passes production auth configuration and uses the public health endpoint in Compose", () => {
    const compose = fs.readFileSync(
      path.join(serviceRoot, "..", "..", "docker-compose.yml"),
      "utf8"
    );
    const serviceStart = compose.indexOf("  agent-orchestrator:");
    const serviceEnd = compose.indexOf("\n  openclaw:", serviceStart);
    const service = compose.slice(serviceStart, serviceEnd);

    expect(serviceStart).toBeGreaterThanOrEqual(0);
    expect(serviceEnd).toBeGreaterThan(serviceStart);
    expect(service).toContain("- NODE_ENV=production");
    expect(service).toContain(
      "- MCP_AUTH_TOKEN=${MCP_AUTH_TOKEN:?MCP_AUTH_TOKEN is required}"
    );
    expect(service).toContain("fetch('http://localhost:3001/health')");
    expect(service).not.toContain("/mcp/rpc");
  });

  it("keeps Jest, ts-jest, and Jest types on one compatible major", () => {
    const jestMajor = declaredMajor(packageJson.devDependencies["jest"]);
    const typesMajor = declaredMajor(packageJson.devDependencies["@types/jest"]);

    // The Jest type package must track the Jest runtime major.
    expect(typesMajor).toBe(jestMajor);

    // ts-jest has no v30 release line; its 29.x builds declare peer support
    // for both Jest 29 and 30, so its major intentionally trails Jest's.
    // Assert the installed ts-jest actually supports the Jest major we depend
    // on instead of forcing all three onto an identical major.
    const tsJestPkg = JSON.parse(
      fs.readFileSync(
        path.join(serviceRoot, "node_modules", "ts-jest", "package.json"),
        "utf8"
      )
    ) as { peerDependencies?: { jest?: string } };
    const jestPeerRange = tsJestPkg.peerDependencies?.jest ?? "";
    expect(jestPeerRange).toMatch(new RegExp(`\\^${jestMajor}\\.`));
  });

  it("keeps lockfile root metadata synchronized with package.json", () => {
    const lockRoot = packageLock.packages[""];

    expect(lockRoot.version).toBe(packageJson.version);
    expect(lockRoot.devDependencies).toEqual(packageJson.devDependencies);
  });
});
