"""
Example: Tool Result Caching with AgentX

This example demonstrates how to use caching to improve performance
and reduce redundant operations.
"""

import time
from agentx_dev import (
    InMemoryCache, LRUCache, FileCache,
    cached_tool, get_global_cache
)


# Example 1: Basic caching with decorator
def basic_caching_demo():
    """Demonstrate basic caching with decorator."""
    print("=== Basic Caching Demo ===\n")

    cache = InMemoryCache(default_ttl=300)

    @cached_tool(cache, ttl=60)
    def expensive_operation(query: str) -> str:
        """Simulate an expensive operation."""
        print(f"  [EXECUTING] expensive_operation('{query}')")
        time.sleep(2)  # Simulate expensive operation
        return f"Result for: {query}"

    # First call - cache miss (slow)
    print("First call (cache miss):")
    start = time.time()
    result1 = expensive_operation("test query")
    print(f"  Result: {result1}")
    print(f"  Time: {time.time() - start:.2f}s\n")

    # Second call - cache hit (fast)
    print("Second call (cache hit):")
    start = time.time()
    result2 = expensive_operation("test query")
    print(f"  Result: {result2}")
    print(f"  Time: {time.time() - start:.2f}s\n")

    # Different query - cache miss
    print("Third call with different query (cache miss):")
    start = time.time()
    result3 = expensive_operation("different query")
    print(f"  Result: {result3}")
    print(f"  Time: {time.time() - start:.2f}s\n")

    print("="*50 + "\n")


# Example 2: LRU Cache
def lru_cache_demo():
    """Demonstrate LRU cache with capacity limits."""
    print("=== LRU Cache Demo ===\n")

    cache = LRUCache(capacity=3, default_ttl=300)

    print("Adding 5 items to cache with capacity of 3:\n")

    for i in range(5):
        cache.set(f"key_{i}", f"value_{i}")
        print(f"Added: key_{i} = value_{i}")

    print("\nCache contents (only last 3 items):")
    for i in range(5):
        value = cache.get(f"key_{i}")
        status = "✓" if value else "✗"
        print(f"  {status} key_{i}: {value}")

    print("\n" + "="*50 + "\n")


# Example 3: Persistent File Cache
def file_cache_demo():
    """Demonstrate persistent file-based caching."""
    print("=== File Cache Demo ===\n")

    cache = FileCache(cache_dir=".cache_demo", default_ttl=3600)

    # Store data
    print("Storing data in file cache...")
    cache.set("user_preferences", {"theme": "dark", "lang": "en"})
    cache.set("api_token", "abc123xyz")

    # Retrieve data
    print("Retrieving data from file cache:")
    preferences = cache.get("user_preferences")
    token = cache.get("api_token")

    print(f"  Preferences: {preferences}")
    print(f"  Token: {token}")

    print("\nCache persists across program runs!")
    print("Try running this example again to see cached data.\n")

    # Cleanup
    cache.clear()
    print("Cache cleared.\n")

    print("="*50 + "\n")


# Example 4: Manual cache operations
def manual_cache_demo():
    """Demonstrate manual cache operations."""
    print("=== Manual Cache Operations Demo ===\n")

    cache = InMemoryCache(default_ttl=5)

    # Set values
    cache.set("key1", "value1", ttl=10)
    cache.set("key2", "value2", ttl=2)  # Expires quickly

    print("Cache contents:")
    print(f"  key1: {cache.get('key1')}")
    print(f"  key2: {cache.get('key2')}")

    # Check existence
    print(f"\nkey1 exists: {cache.has('key1')}")
    print(f"key2 exists: {cache.has('key2')}")

    # Simulate time passing
    print("\nWaiting 3 seconds for key2 to expire...")
    time.sleep(3)

    print(f"key1 exists: {cache.has('key1')}")
    print(f"key2 exists (after TTL): {cache.has('key2')}")

    # Statistics
    print(f"\nCache stats: {cache.get_stats()}")

    # Cleanup expired entries
    removed = cache.cleanup_expired()
    print(f"Cleaned up {removed} expired entries")

    # Delete specific key
    cache.delete("key1")
    print(f"Deleted key1, exists: {cache.has('key1')}")

    print("\n" + "="*50 + "\n")


# Example 5: Caching API calls
def api_caching_demo():
    """Demonstrate caching API calls."""
    print("=== API Call Caching Demo ===\n")

    cache = InMemoryCache(default_ttl=60)

    @cached_tool(cache, ttl=30)
    def fetch_user_data(user_id: int) -> dict:
        """Simulate fetching user data from API."""
        print(f"  [API CALL] Fetching user {user_id}...")
        time.sleep(1)  # Simulate network delay
        return {
            "id": user_id,
            "name": f"User {user_id}",
            "email": f"user{user_id}@example.com"
        }

    print("Fetching user data (first time - API call):")
    user1 = fetch_user_data(123)
    print(f"  {user1}\n")

    print("Fetching same user (cached - no API call):")
    user2 = fetch_user_data(123)
    print(f"  {user2}\n")

    print("Fetching different user (API call):")
    user3 = fetch_user_data(456)
    print(f"  {user3}\n")

    print("="*50 + "\n")


# Example 6: Global cache usage
def global_cache_demo():
    """Demonstrate using global cache instance."""
    print("=== Global Cache Demo ===\n")

    cache = get_global_cache()

    # Use global cache across different parts of code
    cache.set("app_config", {"version": "2.5", "debug": False})
    cache.set("session_id", "xyz789")

    print("Global cache contents:")
    print(f"  Config: {cache.get('app_config')}")
    print(f"  Session: {cache.get('session_id')}")

    print("\nGlobal cache is shared across your application!\n")

    print("="*50 + "\n")


def main():
    """Run all examples."""
    print("\n" + "="*50)
    print("AgentX Caching Examples")
    print("="*50 + "\n")

    basic_caching_demo()
    lru_cache_demo()
    file_cache_demo()
    manual_cache_demo()
    api_caching_demo()
    global_cache_demo()

    print("All examples completed!\n")


if __name__ == "__main__":
    main()
