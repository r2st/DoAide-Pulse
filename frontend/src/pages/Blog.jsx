import { useEffect } from "react";
import { Link } from "react-router-dom";
import { staticPosts } from "../data/blogPosts";
import PublicNav from "../components/PublicNav";
import DoAideFooter from "../components/DoAideFooter";
import CrossProductLinks from "../components/CrossProductLinks";

export default function Blog() {
  useEffect(() => {
    document.title = "Blog — Newsletter Tips & Email Marketing Strategies | DoAide Pulse";
    const meta = document.querySelector('meta[name="description"]');
    if (meta) meta.setAttribute("content", "Expert articles on email marketing, newsletter best practices, AI content creation, and subscriber growth strategies for Indian businesses.");
  }, []);

  useEffect(() => {
    const ld = document.createElement("script");
    ld.type = "application/ld+json";
    ld.textContent = JSON.stringify([
      {
        "@context": "https://schema.org",
        "@type": "Blog",
        name: "DoAide Pulse Blog",
        description: "Newsletter tips, email marketing strategies, and AI content creation guides",
        url: "https://pulse.doaide.com/blog",
        publisher: { "@type": "Organization", name: "DoAide Pulse", url: "https://pulse.doaide.com" },
        blogPost: staticPosts.map((p) => ({
          "@type": "BlogPosting",
          headline: p.title,
          description: p.description,
          datePublished: p.published,
          url: `https://pulse.doaide.com/blog/${p.slug}`,
          author: { "@type": "Organization", name: "DoAide Pulse" },
        })),
      },
      {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        itemListElement: [
          { "@type": "ListItem", position: 1, name: "Home", item: "https://pulse.doaide.com" },
          { "@type": "ListItem", position: 2, name: "Blog", item: "https://pulse.doaide.com/blog" },
        ],
      },
    ]);
    document.head.appendChild(ld);
    return () => document.head.removeChild(ld);
  }, []);

  const posts = [...staticPosts].sort((a, b) => b.published.localeCompare(a.published));

  return (
    <div className="min-h-screen bg-canvas text-ink-700">
      <PublicNav />
      <div className="mx-auto max-w-3xl px-4 py-10">
        <div className="text-center mb-10">
          <span className="eyebrow mb-3 block text-brand-500">Blog</span>
          <h1 className="font-display text-3xl md:text-4xl text-ink-900 mb-3">Newsletter insights</h1>
          <p className="text-ink-500 max-w-lg mx-auto">
            Tips, strategies, and best practices for creating newsletters that grow your audience and drive conversions.
          </p>
        </div>
        <div className="space-y-4">
          {posts.map((p) => (
            <Link
              key={p.slug}
              to={`/blog/${p.slug}`}
              className="panel block p-5 transition hover:border-brand-500/30 hover:shadow-lift"
            >
              <div className="flex items-center gap-2 mb-2">
                {p.keywords.slice(0, 2).map((kw) => (
                  <span key={kw} className="chip text-brand-500">
                    {kw}
                  </span>
                ))}
              </div>
              <h2 className="font-semibold text-ink-900">{p.title}</h2>
              <p className="mt-1 text-sm text-ink-500">{p.description}</p>
              <p className="mt-2 text-xs text-ink-400">{p.published}</p>
            </Link>
          ))}
        </div>

        <div className="panel mt-12 p-8 text-center">
          <h2 className="font-display text-xl text-ink-900 mb-3">Ready to put these tips into action?</h2>
          <p className="text-sm text-ink-500 mb-4">Start your AI-powered newsletter today — free, no credit card required.</p>
          <Link to="/" className="btn-primary">
            Get started free
          </Link>
        </div>

        <CrossProductLinks page="blog" />
      </div>
      <DoAideFooter />
    </div>
  );
}
