import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import type { APIContext } from 'astro';

export async function GET(context: APIContext) {
  const posts = (await getCollection('blog')).sort((a, b) =>
    b.data.pubDate.localeCompare(a.data.pubDate),
  );
  return rss({
    title: 'Shuush — Engineering & Benchmarks',
    description: 'Open-source local Superwhisper & Wispr Flow alternative for Linux/Wayland, built on voxtype.',
    site: context.site ?? 'https://shuush.kitsunekode.in',
    items: posts.map((post) => ({
      title: post.data.title,
      pubDate: new Date(post.data.pubDate),
      description: post.data.description,
      link: `/blog/${post.id}/`,
    })),
  });
}
