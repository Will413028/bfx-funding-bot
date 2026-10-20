import en from "../../messages/en.json";

// Unmatched paths render here, outside the locale layout, so the page brings
// its own document and has nothing that needs a script: a static export
// serves it as 404.html. notFound() inside a locale still renders the
// translated [locale]/not-found.
export default function RootNotFound() {
  return (
    <html lang="en" className="dark">
      <body className="font-sans antialiased bg-background text-foreground">
        <div className="flex min-h-screen flex-col items-center justify-center gap-4">
          <h1 className="text-4xl font-bold">404</h1>
          <p className="text-muted-foreground">{en.common.notFound}</p>
          <a href="/" className="text-accent underline">
            {en.common.back}
          </a>
        </div>
      </body>
    </html>
  );
}
