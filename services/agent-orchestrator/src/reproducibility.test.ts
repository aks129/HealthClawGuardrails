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

  it("keeps Jest, ts-jest, and Jest types on one compatible major", () => {
    const majors = ["jest", "ts-jest", "@types/jest"].map((name) =>
      declaredMajor(packageJson.devDependencies[name])
    );

    expect(new Set(majors)).toEqual(new Set([29]));
  });

  it("keeps lockfile root metadata synchronized with package.json", () => {
    const lockRoot = packageLock.packages[""];

    expect(lockRoot.version).toBe(packageJson.version);
    expect(lockRoot.devDependencies).toEqual(packageJson.devDependencies);
  });
});
