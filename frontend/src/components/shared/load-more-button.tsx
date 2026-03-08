"use client";

import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";

interface LoadMoreButtonProps {
  hasMore: boolean;
  isFetchingNextPage: boolean;
  onClick: () => void;
}

export function LoadMoreButton({
  hasMore,
  isFetchingNextPage,
  onClick,
}: LoadMoreButtonProps) {
  if (!hasMore) return null;

  return (
    <div className="flex justify-center pt-4">
      <Button
        variant="ghost"
        size="sm"
        onClick={onClick}
        disabled={isFetchingNextPage}
        className="active:scale-[0.98]"
      >
        {isFetchingNextPage && (
          <Loader2 className="mr-1.5 size-4 animate-spin" />
        )}
        Load More
      </Button>
    </div>
  );
}
