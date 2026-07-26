import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Run an async loader and expose {data, error, loading, reload}.
 *
 *   const { data, loading, reload } = useApi(() => api.listProjects(), []);
 *
 * The loader is called on mount and whenever `deps` change. Late responses from
 * a superseded call are discarded — without that, switching filters quickly
 * lets a slow first request overwrite the fast second one's results.
 */
export function useApi(loader, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);

  const run = useCallback(() => {
    const current = ++generation.current;
    setLoading(true);
    return loader()
      .then((result) => {
        if (current !== generation.current) return;
        setData(result);
        setError(null);
      })
      .catch((err) => {
        if (current !== generation.current) return;
        setError(err.message || String(err));
      })
      .finally(() => {
        if (current === generation.current) setLoading(false);
      });
    // The loader is a fresh closure on every render; `deps` is what the caller
    // actually means by "this changed".
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    run();
  }, [run]);

  return { data, error, loading, reload: run, setData };
}
