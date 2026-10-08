import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { staticPosts } from "../data/blogPosts";
import CrossProductLinks from "../components/CrossProductLinks";
import PublicNav from "../components/PublicNav";
import DoAideFooter from "../components/DoAideFooter";
import ShareButtons from "../components/ShareButtons";

export default function BlogPost() {
  const { slug } = useParams();
  const post = staticPosts.find((p) => p.slug === slug);

  useEffect(() => {
    if (post) document.title = `${post.title} | DoAide Pulse`;
    else document.title = "Post not found | DoAide Pulse";
  }, [post]);

  useEffect(() => {
    if (!post) return;
    const script = document.createElement("script");
    script.type = "application/ld+json";
    script.textContent = JSON.stringify([
      {
        "@context": "https://schema.org",
        "@type": "Article",
        headline: post.title,
        description: post.description,
        datePublished: post.published,
        keywords: post.keywords.join(", "),
        author: { "@type": "Organization", name: "DoAide Pulse" },
        publisher: {
          "@type": "Organization",
          name: "DoAide Pulse",
          url: "https://pulse.doaide.com",
        },
        mainEntityOfPage: {
          "@type": "WebPage",
          "@id": `https://pulse.doaide.com/blog/${post.slug}`,
        },
      },
      {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        itemListElement: [
          { "@type": "ListItem", position: 1, name: "Home", item: "https://pulse.doaide.com" },
          { "@type": "ListItem", position: 2, name: "Blog", item: "https://pulse.doaide.com/blog" },
          { "@type": "ListItem", position: 3, name: post.title, item: `https://pulse.doaide.com/blog/${post.slug}` },
        ],
      },
    ]);
    document.head.appendChild(script);
    return () => {
      document.head.removeChild(script);
    };
  }, [post]);

  if (!post) {
    return (
      <div className="min-h-screen" style={{ background: "#0A0A0B", color: "#E5E7EB" }}>
        <PublicNav />
        <div className="mx-auto max-w-2xl px-4 py-10">
          <p className="text-[#9CA3AF]">
            Post not found.{" "}
            <Link to="/blog" className="text-[#F0B429] hover:underline">
              Back to blog
            </Link>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen" style={{ background: "#0A0A0B", color: "#E5E7EB" }}>
      <PublicNav />
      <article className="mx-auto max-w-2xl px-4 py-10">
        <nav className="mb-6 font-mono text-[11px] text-[#6B7280]">
          <Link to="/" className="hover:text-[#F0B429]">Home</Link>
          <span className="mx-2">/</span>
          <Link to="/blog" className="hover:text-[#F0B429]">Blog</Link>
          <span className="mx-2">/</span>
          <span className="text-[#9CA3AF]">{post.title}</span>
        </nav>

        <div className="flex flex-wrap gap-2 mb-4">
          {post.keywords.map((kw) => (
            <span key={kw} className="font-mono text-[9px] uppercase tracking-wider text-[#F0B429] bg-[#F0B429]/10 px-2 py-0.5 rounded">
              {kw}
            </span>
          ))}
        </div>

        <h1 className="font-display text-3xl md:text-4xl text-white mb-2 leading-tight">{post.title}</h1>
        <time className="text-xs text-[#6B7280]">{post.published}</time>

        <div className="mt-8 space-y-5">
          {post.body.map((para, i) => (
            <p key={i} className="text-[15px] leading-[1.8] text-[#C4C4CC]">{para}</p>
          ))}
        </div>

        <div className="mt-12 rounded-2xl border border-[#2A2A2D] bg-[#1A1A1D] p-6 text-center">
          <p className="mb-3 text-white font-medium">
            Build newsletters that convert — powered by AI.
          </p>
          <Link to="/" className="inline-block rounded-lg bg-[#F0B429] px-6 py-2.5 text-sm font-medium text-[#0A0A0B] transition hover:bg-[#D4A017]">
            Try DoAide Pulse
          </Link>
        </div>

        <div className="mt-6">
          <ShareButtons text={`${post.title} — read on DoAide Pulse`} />
        </div>
        <CrossProductLinks page="blog" />
      </article>
      <DoAideFooter />
    </div>
  );
}
