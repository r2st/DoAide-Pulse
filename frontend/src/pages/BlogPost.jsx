import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { staticPosts } from "../data/blogPosts";
import PublicNav from "../components/PublicNav";
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
    script.textContent = JSON.stringify({
      "@context": "https://schema.org",
      "@type": "Article",
      headline: post.title,
      description: post.description,
      datePublished: post.published,
      author: { "@type": "Organization", name: "DoAide Pulse" },
      publisher: {
        "@type": "Organization",
        name: "DoAide Pulse",
        url: "https://pulse.doaide.com",
      },
    });
    document.head.appendChild(script);
    return () => {
      document.head.removeChild(script);
    };
  }, [post]);

  if (!post) {
    return (
      <>
        <PublicNav />
        <div className="mx-auto max-w-2xl px-4 py-10">
          <p className="text-ink-700">
            Post not found.{" "}
            <Link to="/blog" className="text-brand-500">
              Back to blog
            </Link>
          </p>
        </div>
      </>
    );
  }

  return (
    <>
      <PublicNav />
      <article className="mx-auto max-w-2xl px-4 py-10">
        <h1 className="page-title mb-2">{post.title}</h1>
        <time className="text-xs text-ink-400">{post.published}</time>
        <div className="prose-herald mt-6">
          {post.body.map((para, i) => (
            <p key={i}>{para}</p>
          ))}
        </div>
        <div className="panel mt-10 p-5 text-center">
          <p className="mb-3 text-ink-900">
            Build newsletters that convert — powered by AI.
          </p>
          <Link to="/" className="btn-primary">
            Try DoAide Pulse
          </Link>
        </div>
        <div className="mt-6">
          <ShareButtons text={`${post.title} — read on DoAide Pulse`} />
        </div>
      </article>
    </>
  );
}
