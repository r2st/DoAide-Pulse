import { useEffect } from "react";
import { Link } from "react-router-dom";
import { staticPosts } from "../data/blogPosts";
import PublicNav from "../components/PublicNav";

export default function Blog() {
  useEffect(() => {
    document.title = "Blog | DoAide Pulse";
  }, []);

  const posts = [...staticPosts].sort((a, b) => b.published.localeCompare(a.published));

  return (
    <>
      <PublicNav />
      <div className="mx-auto max-w-3xl px-4 py-10">
        <h1 className="page-title mb-6">Newsletter insights</h1>
        <div className="space-y-4">
          {posts.map((p) => (
            <Link
              key={p.slug}
              to={`/blog/${p.slug}`}
              className="panel block p-5 hover:border-brand-500 transition"
            >
              <h2 className="font-semibold text-ink-900">{p.title}</h2>
              <p className="mt-1 text-sm text-ink-500">{p.description}</p>
              <p className="mt-1 text-xs text-ink-400">{p.published}</p>
            </Link>
          ))}
        </div>
      </div>
    </>
  );
}
