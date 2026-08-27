import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone traces only the files the server actually needs, so the runtime
  // image does not carry node_modules.
  output: "standalone",

  // NOTE: the /api/backend/* -> /api/v1/* hop is NOT a rewrite. Rewrites are
  // evaluated at build time and would bake in whichever BACKEND_INTERNAL_URL was
  // set when the image was built. It lives in app/api/backend/[...path]/route.ts,
  // which reads the variable per request.

  experimental: {
    // Next caps a proxied request body at 10 MB and, past that, **truncates
    // rather than erroring** — the route handler forwards the first 10 MB and the
    // backend parses a file with its tail missing. A 32 MB ZIP arrived as a
    // headless archive (a ZIP's central directory is at the END of the file), so
    // sniffing found no format and the upload died as a 422 "file type is not
    // supported" — the wrong sentence about the wrong problem.
    //
    // Deliberately ABOVE the backend's MAX_UPLOAD_MB (80), not equal to it. The
    // backend is the authority on how large a book may be, and it is the only
    // layer that can say so in a sentence the reader can act on; this number
    // exists solely to stop the proxy becoming the binding constraint. Keeping
    // headroom means truncation can only ever hit a file the backend was going to
    // reject as too large anyway — never one it would have accepted.
    //
    // Raise this whenever MAX_UPLOAD_MB rises. It is static because the same
    // image must run in every environment (see the route handler's note), so it
    // cannot read the backend's setting.
    proxyClientMaxBodySize: "96mb",
  },
};

export default nextConfig;
