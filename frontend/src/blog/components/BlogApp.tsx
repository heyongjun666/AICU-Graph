// ============================================================================
// BlogApp — Main blog application router and layout
// Handles navigation between all blog pages via path-based routing.
//
// Routes:
//   /                    → ArticleListPage with TagSidebar
//   /articles/:slug      → ArticleDetailPage
//   /articles/new        → ArticleEditorPage (create mode)
//   /articles/:id/edit   → ArticleEditorPage (edit mode)
//   /tags                → TagManagementPage
//   /stats               → StatsDashboardPage
// ============================================================================

import React, { useState, useCallback, useMemo } from 'react';
import { ArticleListPage } from './ArticleListPage';
import { ArticleDetailPage } from './ArticleDetailPage';
import { ArticleEditorPage } from './ArticleEditorPage';
import { TagSidebar } from './TagSidebar';
import { TagManagementPage } from './TagManagementPage';
import { StatsDashboardPage } from '../stats/components/StatsDashboardPage';
import { useArticleList } from './useArticleList';

type Route =
  | { type: 'list' }
  | { type: 'detail'; slug: string }
  | { type: 'create' }
  | { type: 'edit'; id: number }
  | { type: 'tags' }
  | { type: 'stats' };

function parsePath(path: string): Route {
  if (path === '/') return { type: 'list' };
  if (path === '/articles/new') return { type: 'create' };
  if (path.startsWith('/articles/') && path.endsWith('/edit')) {
    const idStr = path.split('/')[2];
    const id = parseInt(idStr, 10);
    if (!isNaN(id)) return { type: 'edit', id };
  }
  if (path.startsWith('/articles/')) {
    const slug = path.slice('/articles/'.length);
    if (slug && slug !== 'new') return { type: 'detail', slug };
  }
  if (path === '/tags') return { type: 'tags' };
  if (path === '/stats') return { type: 'stats' };
  return { type: 'list' };
}

/**
 * Top navigation bar for the blog application.
 */
function BlogNav({ currentPath, onNavigate }: { currentPath: string; onNavigate: (path: string) => void }) {
  const navItems = [
    { path: '/', label: '文章', icon: 'M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z' },
    { path: '/tags', label: '标签', icon: 'M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z' },
    { path: '/stats', label: '统计', icon: 'M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z' },
  ];

  return (
    <nav className="bg-white border-b border-gray-200 shadow-sm">
      <div className="max-w-6xl mx-auto px-4 flex items-center justify-between h-14">
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => onNavigate('/')}
            className="text-lg font-bold text-gray-900 mr-6 hover:text-blue-600 transition-colors"
          >
            My Blog
          </button>
          {navItems.map((item) => (
            <button
              key={item.path}
              type="button"
              onClick={() => onNavigate(item.path)}
              className={`inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                currentPath === item.path
                  ? 'bg-blue-50 text-blue-700'
                  : 'text-gray-600 hover:text-gray-900 hover:bg-gray-50'
              }`}
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={item.icon} />
              </svg>
              {item.label}
            </button>
          ))}
        </div>

        {/* New article button */}
        <button
          type="button"
          onClick={() => onNavigate('/articles/new')}
          className="inline-flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          写文章
        </button>
      </div>
    </nav>
  );
}

/**
 * BlogApp — Entry point for the blog frontend.
 * Manages client-side routing and renders the appropriate page.
 */
export function BlogApp() {
  const [currentPath, setCurrentPath] = useState('/');
  const route = useMemo(() => parsePath(currentPath), [currentPath]);

  // useArticleList is lifted to this level so TagSidebar and ArticleListPage
  // share the same filter/pagination state.
  const listHook = useArticleList();

  const handleNavigate = useCallback((path: string) => {
    setCurrentPath(path);
  }, []);

  const renderPage = () => {
    switch (route.type) {
      case 'list':
        return (
          <div className="flex gap-6 max-w-6xl mx-auto px-4 py-6">
            <TagSidebar
              activeSlug={listHook[0].activeTagSlug}
              onChange={listHook[1].setTagFilter}
            />
            <div className="flex-1 min-w-0">
              <ArticleListPage
                onNavigate={handleNavigate}
                externalState={listHook[0]}
                externalActions={listHook[1]}
                hideTagTabs
              />
            </div>
          </div>
        );

      case 'detail':
        return (
          <ArticleDetailPage
            slug={route.slug}
            onNavigate={handleNavigate}
          />
        );

      case 'create':
        return (
          <ArticleEditorPage onNavigate={handleNavigate} />
        );

      case 'edit':
        return (
          <ArticleEditorPage
            articleId={route.id}
            onNavigate={handleNavigate}
          />
        );

      case 'tags':
        return (
          <TagManagementPage onNavigate={handleNavigate} />
        );

      case 'stats':
        return (
          <StatsDashboardPage onNavigate={handleNavigate} />
        );

      default:
        return null;
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <BlogNav currentPath={currentPath} onNavigate={handleNavigate} />
      <main>
        {renderPage()}
      </main>
    </div>
  );
}
