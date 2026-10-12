output "table_name" {
  value = aws_dynamodb_table.food_products.name
}

output "table_arn" {
  value = aws_dynamodb_table.food_products.arn
}

output "index_name" {
  value = "special_GSI"
}

output "product_count" {
  value = length(aws_dynamodb_table_item.products)
}
