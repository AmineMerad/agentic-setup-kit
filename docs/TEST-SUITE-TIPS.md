# Make the test suite cheap enough that agents run it

Verification only works if the checks are fast. If a suite takes minutes, agents (and humans) skip it.
These were the highest-impact fixes in a Node/TypeScript + Jest + MongoDB codebase this kit was extracted
from: full suite **2 m 27 s → 8 s**, total CPU 127 s → 16 s. Measure before copying any of it.

## 1. Profile first — it was one thing
Per-test durations were ~400 ms each, spread evenly across every file, so splitting the "slowest file" would
have gained nothing. The cause was password hashing: `bcryptjs` (pure JS) at 12 rounds ≈ 350 ms per hash vs
≈ 2 ms at 4 rounds, and every test created users.

```ts
// production stays 12; tests use the minimum
const BCRYPT_ROUNDS = process.env.NODE_ENV === 'test' ? 4 : 12
```
Never assert on the cost factor in tests.

## 2. Don't let a flag silently force serial runs
Jest's `detectOpenHandles` implies `--runInBand`, so `maxWorkers` was ignored and the suite ran one file at
a time. Keep it out of the default config; offer it as an opt-in script (`jest --detectOpenHandles
--runInBand`) for debugging.

## 3. Parallel suites need isolated data
Suites that share one test database clobber each other (flaky, run-to-run varying failures). Give each Jest
worker its own database name on the same server:

```ts
const dbName = `test_worker_${process.env.JEST_WORKER_ID || '0'}`
await mongoose.connect(uri, { dbName })
```
Suites within one worker run sequentially, so they can share that worker's database.

## 4. Don't mask leaked handles with `forceExit`
`forceExit: true` hid a module-level `setInterval` that kept a worker alive; Jest then killed the worker and
reported the *suite* as failed although every test passed. Fix the leak (`.unref()` the timer) and drop
`forceExit`, so a real leak hangs loudly instead of failing randomly.

## 5. If you speed up compilation, check types separately
ts-jest with `isolatedModules: true` skips type-checking, which is much faster — but then Jest no longer
reports type errors, and a `tsconfig` that excludes tests means nothing does. Add a tests tsconfig and run it
in the same `typecheck` script CI already calls:

```jsonc
// tsconfig.test.json
{ "extends": "./tsconfig.json",
  "compilerOptions": { "noEmit": true, "types": ["node", "jest"] },
  "include": ["src/**/*.test.ts", "src/**/__tests__/**/*.ts", "tests/**/*.ts"],
  "exclude": ["node_modules", "dist"] }
```
```jsonc
"typecheck": "tsc --noEmit && tsc -p tsconfig.test.json"
```
The first run of this found a real type error that had been sitting in the test suite.

## 6. Give agents a "related tests" command
`jest --findRelatedTests <files>` runs only suites that import the changed files. Wrap it as a script
(`"test:related": "jest --passWithNoTests --findRelatedTests"`) and point `verify.json` at it. The full suite
is then a last gate for large PRs or shared-infrastructure changes.
