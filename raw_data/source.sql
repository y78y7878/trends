-- --------------------------------------------------------
-- 主機:                           127.0.0.1
-- 伺服器版本:                        12.3.2-MariaDB - MariaDB Server
-- 伺服器作業系統:                      Win64
-- HeidiSQL 版本:                  12.17.0.7270
-- --------------------------------------------------------

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET NAMES utf8 */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;


-- 傾印 trends 的資料庫結構
CREATE DATABASE IF NOT EXISTS `trends` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci */;
USE `trends`;

-- 傾印  資料表 trends.google_trends 結構
CREATE TABLE IF NOT EXISTS `google_trends` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `keyword` varchar(255) NOT NULL,
  `approx_traffic` varchar(50) DEFAULT NULL,
  `published_at` datetime DEFAULT NULL,
  `fetched_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_keyword` (`keyword`),
  KEY `idx_published` (`published_at`)
) ENGINE=InnoDB AUTO_INCREMENT=2321 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 trends.google_trends_news 結構
CREATE TABLE IF NOT EXISTS `google_trends_news` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `trend_id` int(11) NOT NULL,
  `news_title` varchar(500) DEFAULT NULL,
  `news_url` varchar(1000) DEFAULT NULL,
  `news_source` varchar(255) DEFAULT NULL,
  `fetched_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `trend_id` (`trend_id`),
  CONSTRAINT `1` FOREIGN KEY (`trend_id`) REFERENCES `google_trends` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=4265 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 trends.ptt_trends_key_words 結構
CREATE TABLE IF NOT EXISTS `ptt_trends_key_words` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `platform` varchar(50) NOT NULL,
  `category` varchar(100) DEFAULT NULL,
  `title` varchar(500) NOT NULL,
  `keyword` varchar(255) DEFAULT NULL,
  `content_url` varchar(1000) DEFAULT NULL,
  `author` varchar(255) DEFAULT NULL,
  `engagement_score` int(11) DEFAULT 0,
  `published_at` datetime DEFAULT NULL,
  `fetched_at` datetime DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_platform_fetched` (`platform`,`fetched_at`),
  KEY `idx_title` (`title`)
) ENGINE=InnoDB AUTO_INCREMENT=630 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 取消選取資料匯出。

/*!40103 SET TIME_ZONE=IFNULL(@OLD_TIME_ZONE, 'system') */;
/*!40101 SET SQL_MODE=IFNULL(@OLD_SQL_MODE, '') */;
/*!40014 SET FOREIGN_KEY_CHECKS=IFNULL(@OLD_FOREIGN_KEY_CHECKS, 1) */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40111 SET SQL_NOTES=IFNULL(@OLD_SQL_NOTES, 1) */;
