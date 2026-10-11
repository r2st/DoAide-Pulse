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
    const schemas = [
      {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
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
    ];
    if (post.faqs && post.faqs.length > 0) {
      schemas.push({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        mainEntity: post.faqs.map((faq) => ({
          "@type": "Question",
          name: faq.question,
          acceptedAnswer: {
            "@type": "Answer",
            text: faq.answer,
          },
        })),
      });
    }
    const script = document.createElement("script");
    script.type = "application/ld+json";
    script.textContent = JSON.stringify(schemas);
    document.head.appendChild(script);
    return () => {
      document.head.removeChild(script);
    };
  }, [post]);

  if (!post) {
    return (
      <div className="min-h-screen bg-canvas text-ink-700">
        <PublicNav />
        <div className="mx-auto max-w-2xl px-4 py-10">
          <p className="text-ink-500">
            Post not found.{" "}
            <Link to="/blog" className="text-brand-500 hover:underline">
              Back to blog
            </Link>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-canvas text-ink-700">
      <PublicNav />
      <article className="mx-auto max-w-2xl px-4 py-10">
        <nav aria-label="Breadcrumb" className="mb-6 font-mono text-[11px] text-ink-400">
          <Link to="/" className="hover:text-brand-500">Home</Link>
          <span className="mx-2" aria-hidden="true">/</span>
          <Link to="/blog" className="hover:text-brand-500">Blog</Link>
          <span className="mx-2" aria-hidden="true">/</span>
          <span className="text-ink-500" aria-current="page">{post.title}</span>
        </nav>

        <div className="flex flex-wrap gap-2 mb-4">
          {post.keywords.map((kw) => (
            <span key={kw} className="chip text-brand-500">
              {kw}
            </span>
          ))}
        </div>

        <h1 className="font-display text-3xl md:text-4xl text-ink-900 mb-2 leading-tight">{post.title}</h1>
        <time className="text-xs text-ink-400">{post.published}</time>

        <div className="mt-8 space-y-5">
          {post.body.map((para, i) => (
            <p key={i} className="text-[15px] leading-[1.8] text-ink-700">{para}</p>
          ))}
        </div>

        {post.faqs && post.faqs.length > 0 && (
          <section className="mt-10">
            <h2 className="text-xl font-display text-ink-900 mb-4">Frequently Asked Questions</h2>
            <dl className="space-y-4">
              {post.faqs.map((faq, i) => (
                <div key={i} className="panel p-4">
                  <dt className="text-[15px] font-medium text-ink-900 mb-2">{faq.question}</dt>
                  <dd className="text-[14px] leading-[1.7] text-ink-700">{faq.answer}</dd>
                </div>
              ))}
            </dl>
          </section>
        )}

        <div className="panel mt-12 p-6 text-center">
          <p className="mb-3 text-ink-900 font-medium">
            Build newsletters that convert — powered by AI.
          </p>
          <Link to="/" className="btn-primary">
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
