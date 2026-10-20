import { redirect } from "next/navigation";

type Props = {
  params: Promise<{ locale: string }>;
};

// The operator UI has no public landing page, so the locale root opens the
// overview. A static export cannot redirect; once the web API serves the
// export, it sends `/` to the overview itself and this page goes away.
export default async function LocaleRootPage({ params }: Props) {
  const { locale } = await params;
  redirect(`/${locale}/overview`);
}
